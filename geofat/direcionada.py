"""Análise direcionada: procura imóveis-alvo de uma planilha nas fotos do Mapillary e passa só essas fotos pelo
pipeline (detecção, associação geográfica, verificação da caixa e classificação pelo Bonsai).

Uma foto perto do alvo não confirma nada. A foto só é baixada se o alvo cai no campo de visão da câmera; a
observação só fica 'provável' quando a linha de visada da caixa detectada atinge primeiro a edificação do alvo e o
verificador não recusa a caixa. Mesmo assim tudo fica pendente de revisão humana. Da planilha, a IA não recebe
nada: nem categoria, nem matrícula; só o recorte da foto."""
import collections
import math
import time
import urllib.error

from psycopg.types.json import Jsonb

from .classificar import PROMPT, interpretar, recortar
from .config import FOTOS_DIR
from .db import registrar
from .detectar import filtrar_caixas
from .fotos import abrir
from .localizar import ALCANCE_M, ERRO_BUSSOLA, candidatas, coluna_do_angulo, localizar
from .prerotulagem import PROMPT_CAIXA, iou, veredito_caixa
from .tarefas import ContextoConsole
from .trajetorias import distancia_m

FOTOS_POR_ALVO, MAX_FOTOS_POR_ALVO = 3, 10
MAX_ALVOS = 50             # por análise: lote grande só depois de medir o tempo real (README)
DIST_EDIFICACAO_M = 15     # coordenada do cadastro costuma cair na calçada ou no lote vizinho
DIST_MIN_FOTOS_M = 4       # fotos escolhidas para o mesmo alvo ficam pelo menos a isso uma da outra
ORDEM_SITUACAO = {"no_campo": 0, "na_borda": 1, "obstruido": 2}  # as demais não são baixadas


class LimiteAtingido(ValueError):
    pass


def _sinal(a):
    return (a + 180) % 360 - 180


def area_ao_redor(lat, lon, metros):
    """(oeste, sul, leste, norte) de um quadrado de lado 2 × metros, para a busca no Mapillary."""
    dlat = metros / 111_320
    dlon = dlat / math.cos(math.radians(lat))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


# ---------------- Alvos ----------------

def importar_alvos(conn, caminho, origem, quantidade, semente=1, localidade=None, matriculas=None, token=None,
                   raio_m=ALCANCE_M, ctx=None):
    """Lê a planilha e cadastra só `quantidade` alvos (com o token, só os que têm imagem do Mapillary no raio).
    Matrícula já cadastrada não é duplicada."""
    from .mapillary import buscar
    from .planilha import ler_alvos, selecionar

    ctx = ctx or ContextoConsole()
    ctx.progresso(0, None, "Lendo a planilha", "alvos")
    validos, invalidos = ler_alvos(caminho)
    ctx.log(f"{len(validos)} linha(s) com coordenada válida, {invalidos} sem; serão usadas no máximo {quantidade}")
    cobertura = None
    if token:
        def cobertura(a):
            ctx.verificar()
            return len(buscar(token, area_ao_redor(a["lat"], a["lon"], raio_m), limite=20))
    escolhidos, sem_foto = selecionar(validos, quantidade, semente, localidade, matriculas, cobertura)
    ids = []
    for a in escolhidos:
        r = conn.execute("""INSERT INTO alvo (matricula, origem, linha, localidade, municipio, geom)
                            VALUES (%s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
                            ON CONFLICT (matricula) DO UPDATE SET matricula = excluded.matricula RETURNING id""",
                         (a["matricula"], origem, a["linha"], a["localidade"], a["municipio"], a["lon"], a["lat"])).fetchone()
        ids.append(r["id"])
    registrar(conn, "alvo", 0, "importacao", {"origem": origem, "linhas_validas": len(validos), "invalidas": invalidos,
                                              "selecionados": len(ids), "sem_foto_no_raio": len(sem_foto),
                                              "semente": semente, "localidade": localidade,
                                              "matriculas_pedidas": len(matriculas or [])})
    conn.commit()
    return {"mensagem": f"{len(ids)} alvo(s) cadastrado(s) de {len(validos)} linha(s) válida(s)"
                        + (f"; {len(sem_foto)} sorteado(s) sem imagem do Mapillary no raio" if sem_foto else ""),
            "alvo_ids": ids}


def garantir_poligonos(conn, alvo_ids, ctx):
    """Alvos sem polígono do Open Buildings a 150 m (o OSM é esparso fora do Centro): importa Open Buildings e
    OSM da área deles, uma vez para todos. Falha do OSM (Overpass sobrecarregado) só fica no log."""
    from .edificacoes import importar_open_buildings, importar_osm

    sem = conn.execute("""SELECT ST_Y(a.geom) AS lat, ST_X(a.geom) AS lon FROM alvo a WHERE a.id = ANY (%s)
                          AND NOT EXISTS (SELECT 1 FROM edificacao e WHERE e.fonte = 'open_buildings'
                                          AND ST_DWithin(e.geom::geography, a.geom::geography, 150))""",
                       (alvo_ids,)).fetchall()
    if not sem:
        return
    caixas = [area_ao_redor(p["lat"], p["lon"], 300) for p in sem]
    area = (min(c[1] for c in caixas), min(c[0] for c in caixas), max(c[3] for c in caixas), max(c[2] for c in caixas))
    ctx.progresso(0, None, f"Importando polígonos de edificações ({len(sem)} alvo(s) sem polígono por perto)")
    for nome, importar in (("Open Buildings", importar_open_buildings), ("OpenStreetMap", importar_osm)):
        try:
            ctx.log(f"{nome}: {importar(conn, area=area)} polígono(s) na área dos alvos")
        except (OSError, ValueError) as e:  # urllib.error.URLError é OSError
            conn.rollback()
            ctx.log(f"{nome} indisponível agora ({e}); seguindo com os polígonos que houver")


def edificacao_do_alvo(conn, alvo_id):
    """Polígono do alvo: o que contém a coordenada ou o mais próximo até DIST_EDIFICACAO_M. `mesmas` são os ids
    que representam a mesma edificação (OSM e Open Buildings sobrepostos)."""
    r = conn.execute("""
        WITH a AS (SELECT geom FROM alvo WHERE id = %(id)s),
             c AS (SELECT e.id, e.fonte, e.geom, ST_Contains(e.geom, a.geom) AS contem,
                          ST_Distance(e.geom::geography, a.geom::geography) AS d
                   FROM edificacao e, a WHERE ST_DWithin(e.geom::geography, a.geom::geography, %(dist)s)),
             p AS (SELECT * FROM c ORDER BY contem DESC, d LIMIT 1)
        SELECT p.id, p.fonte, p.contem, round(p.d::numeric, 1)::float AS distancia_m,
               ARRAY(SELECT e.id FROM edificacao e WHERE ST_Intersects(e.geom, p.geom)
                     AND ST_Area(ST_Intersection(e.geom, p.geom)) > 0.5 * least(ST_Area(e.geom), ST_Area(p.geom))) AS mesmas,
               (SELECT count(*) FROM c WHERE NOT ST_Intersects(c.geom, p.geom)) AS outras_perto,
               (SELECT count(*) FROM edificacao e, a WHERE ST_DWithin(e.geom::geography, a.geom::geography, 150)) AS na_regiao
        FROM p""", {"id": alvo_id, "dist": DIST_EDIFICACAO_M}).fetchone()
    if not r:
        return None, {"motivo": f"nenhum polígono de edificação a até {DIST_EDIFICACAO_M} m da coordenada"}
    ev = {"edificacao_id": r["id"], "fonte": r["fonte"], "contem_coordenada": r["contem"], "distancia_m": r["distancia_m"],
          "mesma_edificacao": r["mesmas"], "outras_edificacoes_perto": r["outras_perto"],
          "ambigua": not r["contem"] and r["outras_perto"] > 0}
    return r["mesmas"] or [r["id"]], ev


SQL_AVALIAR = """
WITH c AS (SELECT ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) AS cam),
     a AS (SELECT coalesce((SELECT ST_Union(geom) FROM edificacao WHERE id = ANY (%(eds)s::int[])),
                           (SELECT geom FROM alvo WHERE id = %(alvo)s)) AS g),
     v AS (SELECT ST_MakeLine(c.cam, ST_ClosestPoint(a.g, c.cam)) AS linha FROM c, a)
SELECT ST_Distance(a.g::geography, c.cam::geography) AS distancia_m, ST_Contains(a.g, c.cam) AS camera_dentro,
       degrees(ST_Azimuth(c.cam, ST_Centroid(a.g))) AS az_centro,
       ARRAY(SELECT degrees(ST_Azimuth(c.cam, (p).geom)) FROM ST_DumpPoints(a.g) p) AS azs,
       ARRAY(SELECT o.id FROM edificacao o, v WHERE o.id <> ALL (%(eds)s::int[]) AND ST_Intersects(o.geom, v.linha)
             AND NOT ST_Contains(o.geom, c.cam) AND ST_Length(ST_Intersection(o.geom, v.linha)::geography) > 1) AS obstaculos
FROM c, a"""


def avaliar(conn, alvo_id, eds, lat, lon, direcao, fov, largura, altura, camera=None):
    """(situação, evidências) da câmera em relação ao alvo, antes de baixar a foto. Situações: no_campo,
    na_borda (só dentro da margem de erro da bússola), obstruido (outra edificação na linha até o alvo),
    fora_do_campo, longe, sem_direcao, camera_no_alvo (GPS dentro do polígono)."""
    r = conn.execute(SQL_AVALIAR, {"lat": lat, "lon": lon, "eds": eds or [], "alvo": alvo_id}).fetchone()
    ev = {"distancia_m": round(r["distancia_m"], 1), "fov": round(fov, 1),
          "projecao": camera["tipo"] if camera else "pinhole"}
    if r["camera_dentro"]:
        return "camera_no_alvo", ev
    if direcao is None:
        return "sem_direcao", ev
    if r["distancia_m"] > ALCANCE_M:
        return "longe", ev
    centro = _sinal((r["az_centro"] if r["az_centro"] is not None else direcao) - direcao)
    offs = [_sinal(x - r["az_centro"]) for x in r["azs"] if x is not None] if r["az_centro"] is not None else [0]
    lo, hi, meia = centro + min(offs), centro + max(offs), fov / 2
    ev.update({"direcao_camera": round(direcao, 1), "angulo_alvo": [round(lo, 1), round(hi, 1)]})
    if hi < -meia - ERRO_BUSSOLA or lo > meia + ERRO_BUSSOLA:
        return "fora_do_campo", ev
    v0, v1 = max(lo, -meia), min(hi, meia)
    situacao = "na_borda"
    if v0 <= v1:
        situacao = "no_campo"
        # fração da largura da foto onde o alvo deve aparecer (vale para a miniatura e para o original)
        ev["colunas_previstas"] = [round(coluna_do_angulo(d, largura, fov, camera, altura) / largura, 3) for d in (v0, v1)]
    if r["obstaculos"]:
        ev["obstaculos"] = r["obstaculos"][:5]
        situacao = "obstruido"
    return situacao, ev


def escolher_fotos(avaliadas, quantidade):
    """As melhores situações primeiro, depois as mais perto; sem repetir praticamente o mesmo ponto de vista."""
    escolhidas = []
    for sit, ev, n in sorted((x for x in avaliadas if x[0] in ORDEM_SITUACAO),
                             key=lambda x: (ORDEM_SITUACAO[x[0]], x[1]["distancia_m"])):
        if len(escolhidas) >= quantidade:
            break
        if all(distancia_m((n["lon"], n["lat"]), (e[2]["lon"], e[2]["lat"])) >= DIST_MIN_FOTOS_M for e in escolhidas):
            escolhidas.append((sit, ev, n))
    return escolhidas


# ---------------- Etapas ----------------

def preparar_alvo(conn, analise_id, alvo_id, cfg, token, ctx):
    """Polígono do alvo, imagens do Mapillary no raio, avaliação geométrica de cada uma e download das escolhidas."""
    from .mapillary import baixar_imagens, buscar, fov_da_camera

    inicio = time.time()
    eds, ev_ed = edificacao_do_alvo(conn, alvo_id)
    p = conn.execute("SELECT ST_Y(geom) AS lat, ST_X(geom) AS lon FROM alvo WHERE id = %s", (alvo_id,)).fetchone()
    achadas = buscar(token, area_ao_redor(p["lat"], p["lon"], cfg["raio_m"]), limite=500)
    avaliadas = []
    for n in achadas:
        b = n["bruto"]
        largura, altura = b.get("width") or 4, b.get("height") or 3
        fov = fov_da_camera(n["camera"], largura, altura)
        sit, ev = avaliar(conn, alvo_id, eds, n["lat"], n["lon"], n["angulo"], fov, largura, altura, n["camera"])
        avaliadas.append((sit, ev, n))
    situacoes = dict(collections.Counter(a[0] for a in avaliadas))
    escolhidas = escolher_fotos(avaliadas, cfg["fotos_por_alvo"])
    ids, _, falhas = baixar_imagens(conn, [n for _, _, n in escolhidas], ctx) if escolhidas else ({}, 0, 0)
    for sit, ev, n in escolhidas:
        if n["id"] in ids:
            conn.execute("""INSERT INTO analise_foto (analise_id, alvo_id, foto_id, avaliacao) VALUES (%s, %s, %s, %s)
                            ON CONFLICT DO NOTHING""",
                         (analise_id, alvo_id, ids[n["id"]], Jsonb({"situacao": sit, "mapillary_id": n["id"], **ev})))
    estado = "fotos" if ids else "sem_foto"
    resultado = {"imagens_no_raio": len(achadas), "situacoes": situacoes, "baixadas": len(ids), "falhas_download": falhas,
                 "preparo_s": round(time.time() - inicio, 1)}
    if not ids:
        resultado["situacao"] = "sem_foto"
        resultado["motivo"] = ("nenhuma imagem do Mapillary no raio" if not achadas else
                               "nenhuma imagem com o alvo no campo de visão da câmera; foto perto não basta")
    conn.execute("""UPDATE analise_alvo SET estado = %s, edificacao_id = %s, edificacao_evidencias = %s, buscadas = %s,
                           resultado = %s, atualizado_em = now() WHERE analise_id = %s AND alvo_id = %s""",
                 (estado, eds[0] if eds else None, Jsonb(ev_ed), len(achadas), Jsonb(resultado), analise_id, alvo_id))
    conn.commit()
    ctx.log(f"alvo {alvo_id}: {len(achadas)} imagem(ns) no raio de {cfg['raio_m']} m; {situacoes}; {len(ids)} baixada(s)")


SQL_REUSO = """SELECT f.deteccao, f.bbox, f.verificacao, f.classificacao, f.associacao FROM analise_foto f
                JOIN analise a ON a.id = f.analise_id
                WHERE f.foto_id = %(foto)s AND a.ambiente = (SELECT ambiente FROM analise WHERE id = %(analise)s)
                  AND NOT (f.analise_id = %(analise)s AND f.alvo_id = %(alvo)s) AND {condicao}
                ORDER BY f.analise_id DESC LIMIT 1"""


def _reaproveitar(conn, af, condicao, extra):
    """Resultado da mesma foto (mesmo ambiente) já processada para outro alvo ou noutra análise, para não pagar a
    mesma inferência duas vezes. Com 'reprocessar', a análise refaz tudo (é para comparar)."""
    if conn.execute("SELECT config->>'reprocessar' = 'true' AS r FROM analise WHERE id = %s", (af["analise_id"],)).fetchone()["r"]:
        return None
    return conn.execute(SQL_REUSO.format(condicao=condicao),
                        {"foto": af["foto_id"], "analise": af["analise_id"], "alvo": af["alvo_id"], **extra}).fetchone()


def detectar_foto(conn, af, detector, nome, ctx):
    ja = _reaproveitar(conn, af, "f.deteccao->>'detector' = %(nome)s", {"nome": nome})
    if ja:
        conn.execute("""UPDATE analise_foto SET etapa = 'detectada', deteccao = %s
                        WHERE analise_id = %s AND alvo_id = %s AND foto_id = %s""",
                     (Jsonb({"detector": nome, "caixas": ja["deteccao"]["caixas"], "reaproveitada": True}),
                      af["analise_id"], af["alvo_id"], af["foto_id"]))
        conn.commit()
        ctx.log(f"foto {af['foto_id']}: detecção reaproveitada de outro alvo")
        return
    f = conn.execute("SELECT id, arquivo, detectada FROM foto WHERE id = %s", (af["foto_id"],)).fetchone()
    img = abrir(FOTOS_DIR / f["arquivo"])
    inicio = time.time()
    caixas = filtrar_caixas(detector(img))
    dt = round(time.time() - inicio, 2)
    if not f["detectada"]:  # primeira vez: entra no pipeline normal (Revisão, mapa) como a tarefa "detectar"
        novos = [conn.execute("""INSERT INTO imovel (foto_id, bbox, bbox_detectada, detector, score_deteccao, geom)
                                 SELECT id, %s, %s, %s, %s, geom FROM foto WHERE id = %s RETURNING id""",
                              (b, b, nome, s, f["id"])).fetchone()["id"] for b, s, *_ in caixas or [(None, None)]]
        conn.execute("UPDATE foto SET detectada = true WHERE id = %s", (f["id"],))
        for i in novos:
            localizar(conn, i)
    conn.execute("""UPDATE analise_foto SET etapa = 'detectada', deteccao = %s, tempos = tempos || %s
                    WHERE analise_id = %s AND alvo_id = %s AND foto_id = %s""",
                 (Jsonb({"detector": nome, "caixas": [[b, round(s, 3)] for b, s, *_ in caixas]}), Jsonb({"deteccao_s": dt}),
                  af["analise_id"], af["alvo_id"], af["foto_id"]))
    conn.commit()
    ctx.log(f"foto {f['id']}: {len(caixas)} fachada(s) em {dt} s")


def _sobreposicao(bbox, colunas, largura):
    """Fração da largura da caixa dentro das colunas previstas para o alvo (alvo sem polígono, colunas são um
    ponto: 1 se a caixa contém o ponto)."""
    if not colunas:
        return 0.0
    a, b = colunas[0] * largura, colunas[1] * largura
    if b - a < 1:
        return 1.0 if bbox[0] <= a <= bbox[2] else 0.0
    return max(0.0, min(bbox[2], b) - max(bbox[0], a)) / max(1, bbox[2] - bbox[0])


def associar_foto(conn, af, eds):
    """Para cada caixa, as edificações na linha de visada (mesma lógica de localizar). 'provavel': a primeira
    edificação atingida é a do alvo; 'possivel': a do alvo está no setor de visão (ou, sem polígono, a caixa
    cobre as colunas previstas). Fica a melhor caixa da foto."""
    f = conn.execute("""SELECT f.id AS foto_id, f.direcao, f.fov_horizontal, f.largura, f.altura, f.camera,
                               t.rumo, coalesce(t.rumo_confiavel, false) AS rumo_confiavel, t.lado, t.lado_fonte
                        FROM foto f LEFT JOIN foto_trajeto t ON t.foto_id = f.id WHERE f.id = %s""", (af["foto_id"],)).fetchone()
    colunas = af["avaliacao"].get("colunas_previstas")
    avaliadas = []
    for bbox, score in af["deteccao"]["caixas"]:
        lista, ev = candidatas(conn, {**f, "bbox": bbox})
        primeira = next((c["edificacao_id"] for c in lista if c["evidencias"]["no_raio"]), None)
        ids = [c["edificacao_id"] for c in lista]
        sobre = round(_sobreposicao(bbox, colunas, f["largura"]), 2)
        tipo = None
        if eds and primeira in eds:
            tipo = "provavel"
        elif (eds and set(ids) & set(eds)) or (not eds and sobre >= 0.3):
            tipo = "possivel"
        avaliadas.append({"bbox": bbox, "score": score, "associacao": tipo, "primeira_no_raio": primeira,
                          "candidatas": ids[:6], "sobre_colunas_previstas": sobre, "azimute": ev.get("azimute"),
                          "pontuacao": next((c["pontuacao"] for c in lista if c["edificacao_id"] in (eds or [])), 0)})
    com = [a for a in avaliadas if a["associacao"]]
    melhor = max(com, key=lambda a: (a["associacao"] == "provavel", a["pontuacao"], a["sobre_colunas_previstas"]), default=None)
    imovel_id = None
    if melhor:
        obs = conn.execute("SELECT id, bbox FROM imovel WHERE foto_id = %s AND bbox IS NOT NULL", (f["foto_id"],)).fetchall()
        par = max(obs, key=lambda o: iou(o["bbox"], melhor["bbox"]), default=None)
        imovel_id = par["id"] if par and iou(par["bbox"], melhor["bbox"]) >= 0.5 else None
    conn.execute("""UPDATE analise_foto SET etapa = 'associada', associacao = %s, bbox = %s, imovel_id = %s,
                           deteccao = deteccao || %s WHERE analise_id = %s AND alvo_id = %s AND foto_id = %s""",
                 (melhor["associacao"] if melhor else "nao_observado", melhor and melhor["bbox"], imovel_id,
                  Jsonb({"caixas_avaliadas": avaliadas}), af["analise_id"], af["alvo_id"], af["foto_id"]))
    conn.commit()


def classificar_foto(conn, af, perguntar, nome_modelo, ler_texto, verificar, ctx):
    """Verificação da caixa (uma fachada só?) e classificação do uso, só da caixa associada ao alvo."""
    if af["associacao"] not in ("provavel", "possivel"):
        conn.execute("UPDATE analise_foto SET etapa = 'concluida' WHERE analise_id = %s AND alvo_id = %s AND foto_id = %s",
                     (af["analise_id"], af["alvo_id"], af["foto_id"]))
        conn.commit()
        return
    chave = (af["analise_id"], af["alvo_id"], af["foto_id"])
    ja = _reaproveitar(conn, af, "f.bbox = %(bbox)s::int[] AND f.etapa = 'concluida' AND (f.classificacao->>'modelo' = %(modelo)s "
                                 "OR (f.associacao = 'caixa_recusada' AND %(verificar)s))",
                       {"bbox": af["bbox"], "modelo": nome_modelo, "verificar": verificar})
    if ja:  # mesma caixa da mesma foto: o modelo diria o mesmo (a amostragem varia, mas não vale outra chamada)
        conn.execute("""UPDATE analise_foto SET etapa = 'concluida', associacao = CASE WHEN %s = 'caixa_recusada' THEN %s ELSE associacao END,
                               verificacao = %s, classificacao = %s, deteccao = deteccao || '{"modelo_reaproveitado": true}'
                        WHERE analise_id = %s AND alvo_id = %s AND foto_id = %s""",
                     (ja["associacao"], ja["associacao"], Jsonb(ja["verificacao"]) if ja["verificacao"] else None,
                      Jsonb(ja["classificacao"]) if ja["classificacao"] else None, *chave))
        conn.commit()
        return
    arquivo = conn.execute("SELECT arquivo FROM foto WHERE id = %s", (af["foto_id"],)).fetchone()["arquivo"]
    jpeg = recortar(abrir(FOTOS_DIR / arquivo), af["bbox"])
    verificacao, tempos = None, {}
    if verificar:
        inicio = time.time()
        verificacao = veredito_caixa(perguntar(jpeg, PROMPT_CAIXA))
        tempos["verificacao_s"] = round(time.time() - inicio, 2)
        if verificacao["fachada"] is False or verificacao["problema"] in ("nao_e_fachada", "varias_fachadas"):
            conn.execute("""UPDATE analise_foto SET etapa = 'concluida', associacao = 'caixa_recusada', verificacao = %s,
                                   tempos = tempos || %s WHERE analise_id = %s AND alvo_id = %s AND foto_id = %s""",
                         (Jsonb(verificacao), Jsonb(tempos), *chave))
            conn.commit()
            ctx.log(f"foto {af['foto_id']}: caixa recusada pelo verificador ({verificacao['problema']})")
            return
    prompt, texto = PROMPT, None
    if ler_texto:
        inicio = time.time()
        texto = ler_texto(jpeg).strip()
        tempos["ocr_s"] = round(time.time() - inicio, 2)
        if texto and texto.upper() != "NENHUM":
            prompt += f"\nTexto lido na fachada por OCR (pode ter erros): {texto[:500]}"
    inicio = time.time()
    categoria, unidades, confianca, evidencias, limitacoes = interpretar(perguntar(jpeg, prompt))
    tempos["classificacao_s"] = round(time.time() - inicio, 2)
    classificacao = {"categoria": categoria, "unidades": unidades, "confianca": confianca, "evidencias": evidencias,
                     "limitacoes": limitacoes, "modelo": nome_modelo, "texto_ocr": texto}
    if af["imovel_id"]:  # a observação no pipeline normal recebe a classificação só se ainda não tinha
        conn.execute("""UPDATE imovel SET categoria_ia = %s, unidades_ia = %s, confianca_ia = %s, evidencias_ia = %s,
                               limitacoes_ia = %s, modelo_ia = %s, texto_ocr = %s, classificado_em = now()
                        WHERE id = %s AND categoria_ia IS NULL AND bbox IS NOT DISTINCT FROM %s::int[]""",
                     (categoria, unidades, confianca, evidencias, limitacoes, nome_modelo, texto, af["imovel_id"], af["bbox"]))
    conn.execute("""UPDATE analise_foto SET etapa = 'concluida', verificacao = %s, classificacao = %s, erro = NULL,
                           tempos = tempos || %s WHERE analise_id = %s AND alvo_id = %s AND foto_id = %s""",
                 (Jsonb(verificacao) if verificacao else None, Jsonb(classificacao), Jsonb(tempos), *chave))
    conn.commit()
    ctx.log(f"foto {af['foto_id']}: {categoria} (confiança {confianca:.2f}), {sum(tempos.values()):.0f} s no modelo")


def fechar_alvo(conn, analise_id, alvo_id):
    """Resumo do alvo quando todas as fotos passaram por todas as etapas."""
    fotos = conn.execute("SELECT * FROM analise_foto WHERE analise_id = %s AND alvo_id = %s ORDER BY foto_id",
                         (analise_id, alvo_id)).fetchall()
    if not fotos or any(f["etapa"] != "concluida" for f in fotos):
        return
    cont = collections.Counter(f["associacao"] for f in fotos)
    situacao = ("observado_provavel" if cont["provavel"] else "observado_possivel" if cont["possivel"] else "nao_observado")
    resultado = {"situacao": situacao, "associacoes": dict(cont),
                 "classificacoes": [{"foto_id": f["foto_id"], "imovel_id": f["imovel_id"], "associacao": f["associacao"],
                                     **{k: f["classificacao"][k] for k in ("categoria", "unidades", "confianca")}}
                                    for f in fotos if f["classificacao"]],
                 "revisao": "pendente: confirme na Revisão (associação e classificação são sugestões)"}
    conn.execute("""UPDATE analise_alvo SET estado = 'concluido', resultado = resultado || %s, atualizado_em = now()
                    WHERE analise_id = %s AND alvo_id = %s""", (Jsonb(resultado), analise_id, alvo_id))
    conn.commit()


# ---------------- Execução ----------------

def criar_analise(conn, alvo_ids, cfg, reprocessar=False):
    """Análise no ambiente atual. Alvos já concluídos neste ambiente ficam de fora, salvo `reprocessar`
    (para comparar com outra execução). Retorna (analise_id ou None, alvos pulados)."""
    from .provedores import processamento

    ambiente = processamento(conn)["ambiente"]
    feitos = set() if reprocessar else {r["alvo_id"] for r in conn.execute(
        """SELECT x.alvo_id FROM analise_alvo x JOIN analise a ON a.id = x.analise_id
           WHERE a.ambiente = %s AND x.estado IN ('concluido', 'sem_foto') AND x.alvo_id = ANY (%s)""", (ambiente, alvo_ids))}
    ids = [i for i in dict.fromkeys(alvo_ids) if i not in feitos]
    if len(ids) > MAX_ALVOS:
        raise ValueError(f"no máximo {MAX_ALVOS} alvos por análise")
    if not ids:
        return None, sorted(feitos)
    analise_id = conn.execute("INSERT INTO analise (ambiente, config, alvo_ids) VALUES (%s, %s, %s) RETURNING id",
                              (ambiente, Jsonb({**cfg, "reprocessar": reprocessar}), ids)).fetchone()["id"]
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO analise_alvo (analise_id, alvo_id) VALUES (%s, %s)", [(analise_id, i) for i in ids])
    return analise_id, sorted(feitos)


def executar(conn, ctx, analise_id):
    """Roda as etapas em lotes (todas as fotos passam pela detecção antes do Bonsai): na GPU remota com pouca VRAM
    isso evita trocar de modelo a cada foto. Retomável: refaz só o que não terminou."""
    from .provedores import (RemotoIndisponivel, modelo_da_etapa, obter_classificador, obter_detector, obter_ocr,
                             processamento)

    a = conn.execute("SELECT * FROM analise WHERE id = %s", (analise_id,)).fetchone()
    cfg, proc = a["config"], processamento(conn)
    if proc["ambiente"] != a["ambiente"]:
        raise ValueError(f"esta análise é do ambiente {a['ambiente']} e o ambiente atual é {proc['ambiente']} "
                         "(Modelos > Ambiente de processamento); crie outra análise para comparar")
    inicio = time.time()

    def checar():
        ctx.verificar()
        gasto = a["gasto_s"] + time.time() - inicio
        if cfg.get("tempo_max_min") and gasto >= cfg["tempo_max_min"] * 60:
            raise LimiteAtingido(f"tempo máximo de {cfg['tempo_max_min']} min atingido; nada novo foi enviado. "
                                 "Aumente o limite e use 'Retomar'")
        custo, teto = proc.get("custo_hora_usd"), proc.get("orcamento_usd")
        if a["ambiente"] == "runpod" and custo and teto and gasto / 3600 * custo >= teto:
            raise LimiteAtingido(f"orçamento de US$ {teto} atingido (estimado pelo tempo de processamento a US$ {custo}/h); "
                                 "nada novo foi enviado à GPU. Desligue o pod se não for continuar")

    def pendentes(etapa):
        return conn.execute("""SELECT f.* FROM analise_foto f JOIN analise_alvo x USING (analise_id, alvo_id)
                               WHERE f.analise_id = %s AND f.etapa = %s ORDER BY f.alvo_id, f.foto_id""",
                            (analise_id, etapa)).fetchall()

    try:
        alvos = [r["alvo_id"] for r in conn.execute(
            "SELECT alvo_id FROM analise_alvo WHERE analise_id = %s AND estado IN ('pendente', 'erro') ORDER BY alvo_id",
            (analise_id,))]
        if alvos:
            p = conn.execute("SELECT chave FROM provedor WHERE tipo = 'mapillary'").fetchone()
            if not p or not p["chave"]:
                raise ValueError("Mapillary sem Client Token (Treinamento > Pré-rotulagem automática)")
            garantir_poligonos(conn, alvos, ctx)
            for k, alvo_id in enumerate(alvos):
                checar()
                ctx.progresso(k, len(alvos), f"Fotos candidatas: alvo {k + 1} de {len(alvos)}", "alvos")
                try:
                    preparar_alvo(conn, analise_id, alvo_id, cfg, p["chave"], ctx)
                except (ValueError, urllib.error.URLError, OSError) as e:  # um alvo com problema não para os outros
                    conn.rollback()
                    conn.execute("UPDATE analise_alvo SET estado = 'erro', erro = %s WHERE analise_id = %s AND alvo_id = %s",
                                 (str(e)[:500], analise_id, alvo_id))
                    conn.commit()
                    ctx.log(f"alvo {alvo_id}: {e}")
            from .trajetorias import calcular

            calcular(conn)  # sentido do deslocamento e lado da via das fotos novas, usados na associação
            conn.commit()

        fila = pendentes("avaliada")
        if fila:
            m = modelo_da_etapa(conn, "deteccao")
            detector = obter_detector(m)
            ctx.log(f"Detector: {m['nome']}" + (" na GPU remota" if m.get("remoto") else ""))
            for k, af in enumerate(fila):
                checar()
                ctx.progresso(k, len(fila), f"Detecção: foto {k + 1} de {len(fila)}", "fotos")
                detectar_foto(conn, af, detector, m["nome"], ctx)

        eds = {r["alvo_id"]: r["eds"] for r in conn.execute(
            """SELECT x.alvo_id, (SELECT array_agg(v::int) FROM jsonb_array_elements_text(x.edificacao_evidencias->'mesma_edificacao') v) AS eds
               FROM analise_alvo x WHERE analise_id = %s""", (analise_id,))}
        for af in pendentes("detectada"):
            associar_foto(conn, af, eds.get(af["alvo_id"]))

        fila = pendentes("associada")
        if fila:
            m = modelo_da_etapa(conn, "classificacao")
            mo = modelo_da_etapa(conn, "ocr", obrigatorio=False)
            perguntar, nome = obter_classificador(m) if any(f["associacao"] in ("provavel", "possivel") for f in fila) else (None, None)
            ler_texto = obter_ocr(mo) if mo and perguntar else None
            if perguntar:
                ctx.log(f"Verificação e classificação: {m['nome']}" + (" na GPU remota" if m.get("remoto") else ""))
            for k, af in enumerate(fila):
                checar()
                ctx.progresso(k, len(fila), f"Verificação e classificação: foto {k + 1} de {len(fila)}", "fotos")
                try:
                    classificar_foto(conn, af, perguntar, nome, ler_texto, cfg.get("verificar", True), ctx)
                except urllib.error.URLError as e:
                    if isinstance(e.reason, ConnectionRefusedError):
                        raise ValueError(f"{m['nome']} fora do ar; ligue o servidor e use 'Retomar'") from e
                    raise
                except RemotoIndisponivel:
                    raise
                except ValueError as e:  # resposta ilegível do modelo: fica para a próxima retomada
                    conn.rollback()
                    conn.execute("UPDATE analise_foto SET erro = %s WHERE analise_id = %s AND alvo_id = %s AND foto_id = %s",
                                 (str(e)[:500], af["analise_id"], af["alvo_id"], af["foto_id"]))
                    conn.commit()
                    ctx.log(f"foto {af['foto_id']}: {e}")
        for r in conn.execute("SELECT alvo_id FROM analise_alvo WHERE analise_id = %s AND estado = 'fotos'", (analise_id,)).fetchall():
            fechar_alvo(conn, analise_id, r["alvo_id"])
    finally:
        conn.rollback()
        conn.execute("UPDATE analise SET gasto_s = gasto_s + %s WHERE id = %s", (time.time() - inicio, analise_id))
        conn.commit()
    r = resumo(conn, analise_id)
    return {"mensagem": f"{r['alvos']['concluido']} alvo(s) concluído(s): {r['situacoes']}; "
                        f"{r['fotos']} foto(s), {r['associacoes']}", "analise_id": analise_id}


def resumo(conn, analise_id):
    alvos = collections.Counter({"concluido": 0})
    alvos.update(r["estado"] for r in conn.execute("SELECT estado FROM analise_alvo WHERE analise_id = %s", (analise_id,)))
    situacoes = collections.Counter(r["s"] for r in conn.execute(
        "SELECT resultado->>'situacao' AS s FROM analise_alvo WHERE analise_id = %s AND resultado ? 'situacao'", (analise_id,)))
    fotos = conn.execute("SELECT associacao FROM analise_foto WHERE analise_id = %s", (analise_id,)).fetchall()
    return {"alvos": dict(alvos), "situacoes": dict(situacoes), "fotos": len(fotos),
            "associacoes": dict(collections.Counter(f["associacao"] or "pendente" for f in fotos))}


ETAPAS_TEMPO = ("deteccao_s", "verificacao_s", "ocr_s", "classificacao_s")


def medicoes(conn):
    """Tempos médios medidos por ambiente (s), base da estimativa de trabalho pendente."""
    linhas = conn.execute("""
        SELECT a.ambiente, count(DISTINCT (f.analise_id, f.alvo_id)) AS alvos, count(*) AS fotos,
               avg((x.resultado->>'preparo_s')::float) AS preparo_s,
               avg((f.tempos->>'deteccao_s')::float) AS deteccao_s, avg((f.tempos->>'verificacao_s')::float) AS verificacao_s,
               avg((f.tempos->>'ocr_s')::float) AS ocr_s, avg((f.tempos->>'classificacao_s')::float) AS classificacao_s,
               avg((SELECT sum(v::float) FROM jsonb_each_text(f.tempos) t(k, v))) AS foto_s
        FROM analise_foto f JOIN analise a ON a.id = f.analise_id
        JOIN analise_alvo x ON x.analise_id = f.analise_id AND x.alvo_id = f.alvo_id
        WHERE f.etapa = 'concluida' AND NOT f.deteccao ?| array['reaproveitada', 'modelo_reaproveitado'] GROUP BY a.ambiente""").fetchall()
    return {r["ambiente"]: {k: (round(v, 1) if isinstance(v, float) else v) for k, v in r.items() if k != "ambiente"}
            for r in linhas}


def estimar(conn, analise_id, med=None):
    """Segundos de processamento que faltam nesta análise, pelas médias medidas no mesmo ambiente (None sem medição)."""
    a = conn.execute("SELECT ambiente, config FROM analise WHERE id = %s", (analise_id,)).fetchone()
    m = (med or medicoes(conn)).get(a["ambiente"])
    if not m or m.get("foto_s") is None:
        return None
    alvos = conn.execute("SELECT count(*) AS n FROM analise_alvo WHERE analise_id = %s AND estado IN ('pendente', 'erro')",
                         (analise_id,)).fetchone()["n"]
    fotos = {r["etapa"]: r["n"] for r in conn.execute(
        "SELECT etapa, count(*) AS n FROM analise_foto WHERE analise_id = %s GROUP BY etapa", (analise_id,))}
    modelo = sum(m.get(k) or 0 for k in ETAPAS_TEMPO[1:])
    return round(alvos * ((m["preparo_s"] or 0) + a["config"]["fotos_por_alvo"] * m["foto_s"])
                 + fotos.get("avaliada", 0) * m["foto_s"] + (fotos.get("detectada", 0) + fotos.get("associada", 0)) * modelo)


def comparar(conn, a_id, b_id):
    """Mesmas fotos dos mesmos alvos em duas análises (por exemplo local × Runpod): caixas, associação,
    verificação, categoria e tempos lado a lado."""
    linhas = conn.execute("""
        SELECT x.alvo_id, x.foto_id, x.deteccao AS det_a, y.deteccao AS det_b, x.associacao AS assoc_a, y.associacao AS assoc_b,
               x.verificacao->>'problema' AS verif_a, y.verificacao->>'problema' AS verif_b,
               x.classificacao->>'categoria' AS cat_a, y.classificacao->>'categoria' AS cat_b, x.tempos AS tempos_a, y.tempos AS tempos_b
        FROM analise_foto x JOIN analise_foto y ON y.analise_id = %s AND y.alvo_id = x.alvo_id AND y.foto_id = x.foto_id
        WHERE x.analise_id = %s ORDER BY x.alvo_id, x.foto_id""", (b_id, a_id)).fetchall()
    saida = []
    for r in linhas:
        ca = [c[0] for c in (r["det_a"] or {}).get("caixas", [])]
        cb = [c[0] for c in (r["det_b"] or {}).get("caixas", [])]
        iguais = sum(1 for c in ca if any(iou(c, d) >= 0.5 for d in cb))
        saida.append({"alvo_id": r["alvo_id"], "foto_id": r["foto_id"], "caixas_a": len(ca), "caixas_b": len(cb),
                      "caixas_coincidentes": iguais, "associacao_a": r["assoc_a"], "associacao_b": r["assoc_b"],
                      "verificacao_a": r["verif_a"], "verificacao_b": r["verif_b"], "categoria_a": r["cat_a"], "categoria_b": r["cat_b"],
                      "tempo_a_s": round(sum((r["tempos_a"] or {}).values()), 1), "tempo_b_s": round(sum((r["tempos_b"] or {}).values()), 1)})
    tot = {k: conn.execute("SELECT ambiente, round(gasto_s) AS gasto_s FROM analise WHERE id = %s", (i,)).fetchone()
           for k, i in (("a", a_id), ("b", b_id))}
    return {"a": tot["a"], "b": tot["b"], "fotos": saida,
            "iguais": {"associacao": sum(s["associacao_a"] == s["associacao_b"] for s in saida),
                       "categoria": sum(s["categoria_a"] == s["categoria_b"] for s in saida), "total": len(saida)}}
