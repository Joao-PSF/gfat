"""Fotos de rua do Mapillary (Graph API v4) para os datasets de treino.

O Client Token fica no banco (conexão 'Mapillary'), vai só no cabeçalho das chamadas à graph.mapillary.com e
nunca para o navegador. As miniaturas vêm de URLs assinadas da CDN, baixadas sem o token."""
import collections
import io
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from PIL import Image
from psycopg.types.json import Jsonb

from .config import FOTOS_DIR
from .fotos import fov_horizontal
from .tarefas import ContextoConsole

API = "https://graph.mapillary.com"
LICENCA = "CC BY-SA 4.0"
TERMOS_URL = "https://www.mapillary.com/terms"
# Lido nos Termos de Uso do Mapillary em 28/10/2026; aparece no painel e é gravado a cada importação
CONDICOES = [
    "Imagens de outros usuários são licenciadas sob CC BY-SA 4.0 (Termos, seção 3b): exige atribuição ao autor e ao "
    "Mapillary, e obras derivadas sob a mesma licença.",
    "Uso comercial permitido para melhoria, treino e desenvolvimento de produtos, serviços, mapas e estudos, com "
    "salvaguardas contra reidentificação (seção 12).",
    "Proibido tentar reidentificar pessoas ou desfazer o borramento de rostos e placas (seção 5.1).",
    "Acesso só pela API publicada, sem raspagem; o Mapillary pode limitar ou revogar o token (seções 5.1 e 11).",
    "Imagens servidas a partir de servidor próprio devem exibir o logotipo e um link para o Mapillary (seção 11).",
    "Se um modelo treinado conta como obra derivada (share-alike) não está definido nos termos: consulte o jurídico "
    "antes de distribuir o modelo.",
]
CAMPOS = ("id,captured_at,compass_angle,computed_compass_angle,geometry,computed_geometry,sequence,creator,"
          "is_pano,camera_type,camera_parameters,width,height,thumb_2048_url")
# Medido no Centro de São Luís (28/10/2026): bbox de 0,02° falha com HTTP 500 "reduce the amount of data" e a de
# 0,01° já bate no limite de 2.000 imagens. Célula de 0,005° (~550 m); se ainda assim a API recusar, divide em 4.
LADO_CELULA, LADO_MIN = 0.005, 0.0012
AREA_MAX = 0.001    # grau² por busca (~3,5 × 3,5 km no equador; até 40 consultas)
TESTE_BBOX = (-44.302, -2.532, -44.298, -2.528)  # Centro de São Luís


def _get(caminho, token, timeout=30, **params):
    if not token:
        raise ValueError("Mapillary sem Client Token (Treinamento > Pré-rotulagem automática ou Modelos > Conexões)")
    req = urllib.request.Request(f"{API}/{caminho}?{urllib.parse.urlencode(params)}",
                                 headers={"Authorization": f"OAuth {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            detalhe = json.load(e).get("error", {}).get("message", "")
        except Exception:
            detalhe = ""
        raise ValueError(f"Mapillary respondeu HTTP {e.code}"
                         + (": token inválido ou sem permissão" if e.code in (401, 403) else "")
                         + (f" ({detalhe[:200]})" if detalhe else "")) from e


def validar_bbox(bbox):
    oeste, sul, leste, norte = bbox
    if not (-180 <= oeste < leste <= 180 and -90 <= sul < norte <= 90):
        raise ValueError("área inválida (oeste, sul, leste, norte)")
    if (leste - oeste) * (norte - sul) > AREA_MAX:
        raise ValueError("área grande demais para uma busca: aproxime o mapa (até ~3,5 × 3,5 km)")


def celulas(bbox, lado=LADO_CELULA):
    """Divide a área em consultas que a API aceita."""
    oeste, sul, leste, norte = bbox
    nx, ny = math.ceil((leste - oeste) / lado - 1e-9), math.ceil((norte - sul) / lado - 1e-9)
    dx, dy = (leste - oeste) / nx, (norte - sul) / ny
    return [(oeste + i * dx, sul + j * dy, oeste + (i + 1) * dx, sul + (j + 1) * dy) for j in range(ny) for i in range(nx)]


def camera_de(x):
    """Projeção informada pelo Mapillary (focal relativa ao maior lado, distorção k1, k2) ou None."""
    p = x.get("camera_parameters")
    if x.get("camera_type") in ("fisheye", "perspective") and isinstance(p, list) and len(p) == 3 and p[0] > 0:
        return {"tipo": x["camera_type"], "parametros": [float(v) for v in p]}
    return None


def fov_da_camera(camera, largura, altura):
    """Campo de visão horizontal (graus) pela projeção; sem ela, o padrão de celular de fotos.fov_horizontal."""
    from .localizar import angulo_da_coluna

    if not camera:
        return fov_horizontal(None, largura, altura)
    if camera["tipo"] == "fisheye":
        return 2 * angulo_da_coluna(largura, largura, None, camera, altura)
    return 2 * math.degrees(math.atan(0.5 * largura / max(largura, altura) / camera["parametros"][0]))


def normalizar(x):
    """Campos que o GeoFAT usa; posição e direção recalculadas pelo Mapillary têm prioridade sobre as do GPS."""
    lon, lat = (x.get("computed_geometry") or x["geometry"])["coordinates"]
    angulo = x.get("computed_compass_angle")
    angulo = x.get("compass_angle") if angulo is None else angulo
    criador = x.get("creator") or {}
    return {"id": str(x["id"]), "lat": lat, "lon": lon, "angulo": None if angulo is None else float(angulo) % 360,
            "capturada_em": datetime.fromtimestamp(x["captured_at"] / 1000, timezone.utc) if x.get("captured_at") else None,
            "autor": criador.get("username"), "sequencia": x.get("sequence"),
            "pano": bool(x.get("is_pano")) or x.get("camera_type") in ("spherical", "equirectangular"),
            "camera": camera_de(x),
            "miniatura": x.get("thumb_2048_url")}


def buscar(token, bbox, limite=500, incluir_pano=False):
    """Imagens na área, sem repetir; panorâmicas 360° ficam de fora por padrão. Olho de peixe
    entra: em São Luís quase toda a cobertura é de câmera veicular lateral (Blackvue), com a fachada bem visível."""
    validar_bbox(bbox)
    achadas = {}
    pilha = celulas(bbox)[::-1]
    while pilha:
        c = pilha.pop()
        try:
            dados = _get("images", token, bbox=",".join(f"{v:.6f}" for v in c), fields=CAMPOS, limit=2000)["data"]
        except ValueError as e:
            if "HTTP 500" not in str(e) or c[2] - c[0] < LADO_MIN:
                raise
            pilha.extend(celulas(c, (c[2] - c[0]) / 2)[::-1])  # área densa demais: consulta em 4 partes
            continue
        for x in dados:
            if x.get("geometry") or x.get("computed_geometry"):
                n = normalizar(x)
                if incluir_pano or not n["pano"]:
                    achadas.setdefault(n["id"], {**n, "bruto": x})
        if len(achadas) >= limite:
            break
    return sorted(achadas.values(), key=lambda n: (n["sequencia"] or "", n["capturada_em"] or datetime.min.replace(tzinfo=timezone.utc)))[:limite]


def testar(token):
    """(ok, mensagem) com uma busca real e pequena no Centro de São Luís."""
    try:
        n = len(_get("images", token, timeout=15, bbox=",".join(map(str, TESTE_BBOX)), fields="id", limit=5)["data"])
    except ValueError as e:
        return False, str(e)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, f"sem resposta: {getattr(e, 'reason', e)}"
    return True, f"token aceito; {n} imagem(ns) de teste no Centro de São Luís"


def _baixar(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:  # URL assinada da CDN: sem token
        return r.read()


def importar(conn, token, bbox, limite, dataset_id=None, incluir_pano=False, ctx=None, baixar=_baixar):
    """Baixa as imagens da área para data/fotos/mapillary/, cadastra como fotos (posição, direção, percurso)
    com a procedência e a licença, e põe no dataset. Imagens já baixadas não são baixadas de novo."""
    from .datasets import adicionar_fotos

    ctx = ctx or ContextoConsole()
    ctx.progresso(0, None, "Buscando imagens no Mapillary", "imagens")
    achadas = buscar(token, bbox, limite, incluir_pano)
    ctx.log(f"{len(achadas)} imagem(ns) na área {bbox}")
    ids, novas, falhas = baixar_imagens(conn, achadas, ctx, baixar)
    ctx.progresso(len(achadas), len(achadas), "Montando percursos")
    adicionadas = len(adicionar_fotos(conn, dataset_id, list(ids.values()))) if dataset_id else 0
    from .trajetorias import calcular

    calcular(conn)
    conn.commit()
    return {"mensagem": f"{novas} imagem(ns) nova(s) do Mapillary, {len(ids) - novas} já baixada(s), "
                        f"{falhas} com falha; {adicionadas} adicionada(s) ao dataset",
            "novas": novas, "falhas": falhas, "adicionadas": adicionadas}


def baixar_imagens(conn, achadas, ctx=None, baixar=_baixar):
    """Baixa e cadastra imagens já buscadas (saída de buscar). Retorna ({id Mapillary: foto_id}, novas, falhas).
    Foto já baixada sem a projeção da câmera recebe a projeção e o campo de visão corrigido (com histórico)."""
    from .db import registrar

    ctx = ctx or ContextoConsole()
    pasta = FOTOS_DIR / "mapillary"
    pasta.mkdir(parents=True, exist_ok=True)
    ordem, vistos = {}, collections.Counter()
    # shortcut: o número do quadro é a posição entre as baixadas juntas; baixadas em vezes diferentes da mesma
    # sequência podem ficar fora de ordem na trajetória. A direção da câmera não depende disso.
    for n in achadas:  # já ordenadas por sequência e hora: a posição na sequência vira o número do quadro
        ordem[n["id"]] = vistos[n["sequencia"]]
        vistos[n["sequencia"]] += 1
    agora = datetime.now(timezone.utc)
    foto_ids, novas, falhas = {}, 0, 0
    for k, n in enumerate(achadas):
        ctx.verificar()
        ctx.progresso(k, len(achadas), f"Baixando {k + 1} de {len(achadas)}")
        ja = conn.execute("SELECT foto_id FROM foto_procedencia WHERE fonte = 'mapillary' AND id_externo = %s",
                          (n["id"],)).fetchone()
        if ja:
            foto_ids[n["id"]] = ja["foto_id"]
            f = conn.execute("SELECT largura, altura, fov_horizontal FROM foto WHERE id = %s AND camera IS NULL",
                             (ja["foto_id"],)).fetchone()
            if f and n.get("camera"):
                fov = fov_da_camera(n["camera"], f["largura"], f["altura"])
                conn.execute("UPDATE foto SET camera = %s, fov_horizontal = %s WHERE id = %s",
                             (Jsonb(n["camera"]), fov, ja["foto_id"]))
                registrar(conn, "foto", ja["foto_id"], "projecao_corrigida",
                          {"camera": n["camera"], "fov_anterior": f["fov_horizontal"], "fov": fov})
                conn.commit()
            continue
        if not n["miniatura"]:
            ctx.log(f"{n['id']}: sem miniatura disponível, ignorada")
            falhas += 1
            continue
        arquivo = pasta / f"{n['id']}.jpg"
        try:
            if not arquivo.exists():
                dados = baixar(n["miniatura"])
                Image.open(io.BytesIO(dados)).verify()
                arquivo.write_bytes(dados)  # o original como veio, sem recodificar
            with Image.open(arquivo) as img:
                largura, altura = img.size
        except Exception as e:
            ctx.log(f"{n['id']}: download falhou ({e})")
            falhas += 1
            continue
        credito = f"© {n['autor'] or 'autor não informado'}, Mapillary, {LICENCA}"
        f = conn.execute(
            """INSERT INTO foto (arquivo, fonte, capturada_em, largura, altura, direcao, fov_horizontal, credito,
                                 trajeto, sequencia, camera, geom)
               VALUES (%s, 'mapillary', %s, %s, %s, %s, %s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
               ON CONFLICT (arquivo) DO UPDATE SET arquivo = excluded.arquivo RETURNING id""",
            (f"mapillary/{arquivo.name}", n["capturada_em"].replace(tzinfo=None) if n["capturada_em"] else None,
             largura, altura, n["angulo"], fov_da_camera(n.get("camera"), largura, altura), credito,
             f"mapillary-{n['sequencia']}" if n["sequencia"] else None, ordem.get(n["id"]),
             Jsonb(n["camera"]) if n.get("camera") else None, n["lon"], n["lat"])).fetchone()
        conn.execute("""INSERT INTO foto_procedencia (foto_id, fonte, id_externo, autor, sequencia_externa, licenca,
                                                      termos_url, termos_verificados_em, url_pagina, dados)
                        VALUES (%s, 'mapillary', %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                     (f["id"], n["id"], n["autor"], n["sequencia"], LICENCA, TERMOS_URL, agora,
                      f"https://www.mapillary.com/app/?pKey={n['id']}",
                      Jsonb({c: v for c, v in n["bruto"].items() if c != "thumb_2048_url"})))  # URL assinada expira
        conn.commit()
        foto_ids[n["id"]] = f["id"]
        novas += 1
    return foto_ids, novas, falhas
