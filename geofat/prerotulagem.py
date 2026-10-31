"""Pré-rotulagem automática de fachadas para datasets de treino.

Para cada imagem: o detector propõe caixas; o verificador (Bonsai ou outro VLM) julga cada recorte e conta, na
imagem inteira com as caixas desenhadas, as fachadas que ficaram sem caixa. Com erro, o detector roda de novo com
o limiar ajustado, até o máximo de tentativas configurado. O que passa nos critérios é aceito; o resto vai à
revisão. A aprovação do verificador NÃO mede precisão: uma amostra sorteada antes do processamento vai sem
sugestões para a revisão humana, e é ela que mede o resultado automático."""
import json
import math
import random
import re
import urllib.error

from PIL import ImageDraw, ImageFont
from psycopg.types.json import Jsonb

from .classificar import recortar
from .config import FOTOS_DIR
from .db import registrar
from .detectar import filtrar_caixas
from .fotos import abrir
from .tarefas import Cancelada

PROMPT_CAIXA = """Este é um recorte de uma foto de rua. Ele deveria conter exatamente UMA fachada de imóvel (frente de casa, prédio, loja, galpão ou o muro/portão de um lote), sem pedaços grandes de outras fachadas.
Responda somente com JSON:
{"fachada": true, "problema": "nenhum|nao_e_fachada|varias_fachadas|cortada", "confianca": 0.0, "motivo": "o que você viu"}
- nao_e_fachada: céu, rua, carro, árvore, poste, só um pedaço de parede sem imóvel identificável
- varias_fachadas: a caixa engloba duas ou mais fachadas de imóveis diferentes
- cortada: é uma fachada, mas a caixa corta parte dela"""

PROMPT_OMITIDAS = """Esta foto de rua tem caixas numeradas desenhadas sobre as fachadas de imóveis já detectadas.
Conte as fachadas de imóveis visíveis e razoavelmente inteiras que NÃO têm nenhuma caixa.
Responda somente com JSON: {"fachadas_visiveis": 0, "sem_caixa": 0, "onde": "posição das que faltam"}"""

PROBLEMAS = ("nenhum", "nao_e_fachada", "varias_fachadas", "cortada")
CRITERIOS_PADRAO = {"confianca_verificador": 0.6, "score_detector": 0.25, "omitidas_max": 0}
PASSO_LIMIAR, LIMIAR_MIN, LIMIAR_MAX = 0.08, 0.1, 0.8
MAX_TENTATIVAS = 5  # teto fixo: nenhuma configuração faz o laço passar disso


def ler_json(texto):
    texto = re.sub(r"<think>.*?</think>", "", texto, flags=re.S)
    achado = re.search(r"\{.*\}", texto, re.S)
    if not achado:
        raise ValueError(f"resposta sem JSON: {texto[:200]!r}")
    return json.loads(achado.group())


def veredito_caixa(texto):
    """{'fachada': bool|None, 'problema', 'confianca', 'motivo'}; resposta ilegível vira problema 'resposta_invalida'."""
    try:
        d = ler_json(texto)
        fachada = d.get("fachada")
        fachada = fachada if isinstance(fachada, bool) else str(fachada).strip().lower() in ("true", "sim", "yes")
        problema = str(d.get("problema") or "nenhum").strip().lower().replace(" ", "_")
        problema = problema if problema in PROBLEMAS else ("nenhum" if fachada else "nao_e_fachada")
        if not fachada and problema == "nenhum":
            problema = "nao_e_fachada"
        return {"fachada": fachada, "problema": problema, "confianca": min(1.0, max(0.0, float(d.get("confianca") or 0))),
                "motivo": str(d.get("motivo") or "")[:500]}
    except (ValueError, TypeError) as e:
        return {"fachada": None, "problema": "resposta_invalida", "confianca": 0.0, "motivo": str(e)[:300]}


def ler_omitidas(texto):
    """Fachadas sem caixa segundo o verificador; None se a resposta não serve."""
    try:
        v = ler_json(texto).get("sem_caixa")
        return max(0, int(v)) if isinstance(v, (int, float)) or str(v).isdigit() else None
    except (ValueError, TypeError):
        return None


def decidir(caixas, vereditos, omitidas, criterios):
    """(decisão, problema, caixas confirmadas, caixas recusadas). Confirmada = o verificador diz que é uma fachada
    (inteira ou cortada) com a confiança mínima, e o detector deu o score mínimo."""
    confirmadas, falsas, fracas = [], [], []
    for c, v in zip(caixas, vereditos):
        if v["fachada"] is False or v["problema"] in ("nao_e_fachada", "varias_fachadas"):
            falsas.append(c)
        elif v["fachada"] and v["confianca"] >= criterios["confianca_verificador"] and c[1] >= criterios["score_detector"]:
            confirmadas.append(c)
        else:
            fracas.append(c)
    # Caixa errada primeiro: baixar o limiar por causa das omitidas só traria mais caixas erradas
    # (visto em rua estreita do Centro: o Grounding DINO agrupava várias casas e o verificador apontava omitidas)
    if falsas:
        problema = "falsos_positivos"
    elif omitidas is None:
        problema = "verificacao_incompleta"
    elif omitidas > criterios["omitidas_max"]:
        problema = "omitidas"
    elif fracas:
        problema = "baixa_confianca"
    elif not caixas:
        problema = "sem_caixas"
    else:
        return "aceita", None, confirmadas, []
    return ("revisar" if confirmadas else "rejeitada"), problema, confirmadas + fracas, falsas


def proximo_limiar(limiar, problema):
    """Limiar da nova detecção, ou None quando outra detecção não resolve o problema."""
    if problema in ("omitidas", "sem_caixas"):
        novo = max(LIMIAR_MIN, limiar - PASSO_LIMIAR)
    elif problema == "falsos_positivos":
        novo = min(LIMIAR_MAX, limiar + PASSO_LIMIAR)
    else:
        return None
    return None if abs(novo - limiar) < 1e-9 else round(novo, 3)


def desenhar(img, caixas):
    """JPEG da imagem inteira com as caixas numeradas, para o verificador procurar as que faltam."""
    img = img.copy()
    d, esp = ImageDraw.Draw(img), max(3, img.width // 300)
    try:
        fonte = ImageFont.load_default(size=max(16, img.width // 40))
    except TypeError:  # Pillow antigo
        fonte = ImageFont.load_default()
    for n, (bbox, *_) in enumerate(caixas, 1):
        d.rectangle(bbox, outline=(255, 0, 0), width=esp)
        d.text((bbox[0] + esp * 2, bbox[1] + esp * 2), str(n), fill=(255, 255, 0), font=fonte, stroke_width=2, stroke_fill=(0, 0, 0))
    return recortar(img, None)


def pre_rotular_imagem(img, detector, perguntar, texto_busca, config, verificar=lambda: None):
    """Tentativas (uma por detecção) e a melhor delas. Não toca no banco."""
    limiar, tentativas = config["limiar_inicial"], []
    for n in range(1, min(config["max_tentativas"], MAX_TENTATIVAS) + 1):
        verificar()
        caixas = [(list(b), round(float(s), 4)) for b, s, *_ in filtrar_caixas(detector(img, {"fachada": texto_busca}, limiar))
                  if b[2] - b[0] >= 8 and b[3] - b[1] >= 8]
        vereditos = []
        for bbox, _ in caixas:
            verificar()
            vereditos.append(veredito_caixa(perguntar(recortar(img, bbox, 0.05), PROMPT_CAIXA)))
        # com caixa recusada a imagem não é aceita nesta tentativa: poupa uma chamada ao verificador (~1,5 min no CPU)
        falsa = any(v["fachada"] is False or v["problema"] in ("nao_e_fachada", "varias_fachadas") for v in vereditos)
        resposta = "" if falsa else perguntar(desenhar(img, caixas), PROMPT_OMITIDAS)
        omitidas = None if falsa else ler_omitidas(resposta)
        decisao, problema, aceitas, recusadas = decidir(caixas, vereditos, omitidas, config["criterios"])
        tentativas.append({"tentativa": n, "parametros": {"limiar": limiar}, "caixas": caixas, "vereditos": vereditos,
                           "omitidas": omitidas, "resposta_omitidas": resposta[:2000], "problema": problema,
                           "decisao": decisao, "aceitas": aceitas, "recusadas": recusadas})
        if decisao == "aceita":
            break
        limiar = proximo_limiar(limiar, problema)
        if limiar is None:
            break
    melhor = max(tentativas, key=lambda t: (t["decisao"] == "aceita", len(t["aceitas"]) - len(t["recusadas"]) - (t["omitidas"] or 0), -t["tentativa"]))
    return tentativas, melhor


def sortear_amostra(imagem_ids, pct, rng=random):
    """Amostra de avaliação, sorteada antes de qualquer resultado: ao menos 1 imagem."""
    if not imagem_ids:
        return []
    return sorted(rng.sample(list(imagem_ids), max(1, math.ceil(len(imagem_ids) * pct / 100))))


def iou(a, b):
    inter = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    uniao = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / uniao if uniao else 0.0


def comparar(auto, humano, limiar_iou=0.5):
    """(acertos, falsos positivos, omitidas) das caixas automáticas contra as da revisão humana."""
    livres, acertos = list(humano), 0
    for a in auto:
        melhor = max(livres, key=lambda h: iou(a, h), default=None)
        if melhor is not None and iou(a, melhor) >= limiar_iou:
            livres.remove(melhor)
            acertos += 1
    return acertos, len(auto) - acertos, len(livres)


def versionar(conn, imagem_id, origem):
    """Guarda as caixas atuais da imagem como uma versão (o editor substitui as caixas a cada salvamento)."""
    conn.execute("""INSERT INTO anotacao_versao (imagem_id, origem, estado, anotacoes)
                    SELECT i.id, %s, i.estado, coalesce(jsonb_agg(jsonb_build_object('classe', a.classe, 'bbox', a.bbox,
                             'origem', a.origem, 'score', a.score, 'modelo', a.modelo) ORDER BY a.id)
                             FILTER (WHERE a.id IS NOT NULL), '[]')
                    FROM dataset_imagem i LEFT JOIN anotacao a ON a.imagem_id = i.id WHERE i.id = %s GROUP BY i.id""",
                 (origem, imagem_id))


def _gravar(conn, p, img, tentativas, melhor, nomes):
    cfg = p["config"]
    for t in tentativas:
        conn.execute("""INSERT INTO verificacao (prerotulagem_id, imagem_id, tentativa, parametros, caixas, vereditos,
                                                 omitidas, resposta_omitidas, problema, detector, verificador)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                     (p["id"], img["id"], t["tentativa"], Jsonb(t["parametros"]), Jsonb(t["caixas"]), Jsonb(t["vereditos"]),
                      t["omitidas"], t["resposta_omitidas"], t["problema"], nomes[0], nomes[1]))
    atual = conn.execute("""SELECT i.estado, i.amostra_avaliacao, d.classes,
                                   exists(SELECT 1 FROM anotacao a WHERE a.imagem_id = i.id AND a.origem <> 'sugestao') AS editada
                            FROM dataset_imagem i JOIN dataset d ON d.id = i.dataset_id WHERE i.id = %s FOR UPDATE OF i""",
                         (img["id"],)).fetchone()
    motivo = melhor["problema"]
    if atual["estado"] == "aprovada" or atual["editada"]:
        motivo = "imagem editada por uma pessoa durante o processo; caixas mantidas"
    else:
        automatica = (melhor["decisao"] == "aceita" and not cfg["revisao_humana"] and not atual["amostra_avaliacao"]
                      and atual["classes"] == ["fachada"])
        conn.execute("DELETE FROM anotacao WHERE imagem_id = %s", (img["id"],))
        # amostra de avaliação: sem sugestões, para a pessoa anotar sem ser influenciada pelo resultado automático
        for bbox, score in ([] if atual["amostra_avaliacao"] else melhor["aceitas"]):
            conn.execute("""INSERT INTO anotacao (imagem_id, classe, bbox, origem, score, modelo)
                            VALUES (%s, 'fachada', %s, %s, %s, %s)""",
                         (img["id"], bbox, "aceita" if automatica else "sugestao", score, f"{nomes[0]} + {nomes[1]}"))
        conn.execute("""UPDATE dataset_imagem SET estado = %s, aprovacao = %s, atualizada_em = now() WHERE id = %s""",
                     ("aprovada" if automatica else "revisar", "automatica" if automatica else None, img["id"]))
        versionar(conn, img["id"], "prerotulagem")
    conn.execute("""INSERT INTO prerotulo (prerotulagem_id, imagem_id, decisao, tentativas, motivo, caixas)
                    VALUES (%s, %s, %s, %s, %s, %s)""",
                 (p["id"], img["id"], melhor["decisao"], len(tentativas), motivo, Jsonb([b for b, _ in melhor["aceitas"]])))
    registrar(conn, "dataset_imagem", img["id"], "prerotulagem",
              {"prerotulagem_id": p["id"], "decisao": melhor["decisao"], "tentativas": len(tentativas), "motivo": motivo})


def executar(conn, ctx, prerotulagem_id):
    """Processa as imagens que ainda não têm resultado nesta pré-rotulagem; repetir a tarefa retoma de onde parou."""
    from .provedores import modelo_por_id, obter_detector, obter_verificador

    p = conn.execute("""SELECT p.*, d.textos_busca FROM prerotulagem p JOIN dataset d ON d.id = p.dataset_id
                        WHERE p.id = %s""", (prerotulagem_id,)).fetchone()
    if not p:
        raise ValueError("pré-rotulagem não encontrada")
    cfg = p["config"]
    pendentes = conn.execute("""SELECT i.id, i.arquivo FROM dataset_imagem i WHERE i.id = ANY (%s)
                                AND NOT EXISTS (SELECT 1 FROM prerotulo r WHERE r.prerotulagem_id = %s AND r.imagem_id = i.id)
                                ORDER BY i.id""", (p["imagem_ids"], p["id"])).fetchall()
    total, feitas = len(p["imagem_ids"]), len(p["imagem_ids"]) - len(pendentes)
    if feitas:
        ctx.log(f"Retomando: {feitas} de {total} imagem(ns) já processada(s)")
    det_m, ver_m = modelo_por_id(conn, cfg["detector_id"]), modelo_por_id(conn, cfg["verificador_id"])
    ctx.progresso(feitas, total, f"Carregando {det_m['nome']} e conectando a {ver_m['nome']}", "imagens")
    detector, perguntar = obter_detector(det_m), obter_verificador(ver_m)
    texto = p["textos_busca"].get("fachada") or "building facade"
    cfg = {**cfg, "limiar_inicial": cfg.get("limiar_inicial") or det_m["config"].get("limiar", 0.35)}
    contagem, falhas = {"aceita": 0, "revisar": 0, "rejeitada": 0}, 0
    for img in pendentes:
        ctx.verificar()
        ctx.progresso(feitas, total, f"Imagem {feitas + 1} de {total}")
        try:
            tentativas, melhor = pre_rotular_imagem(abrir(FOTOS_DIR / img["arquivo"]), detector, perguntar, texto, cfg,
                                                    ctx.verificar)
        except urllib.error.URLError as e:
            if isinstance(e.reason, ConnectionRefusedError):
                raise ValueError(f"{ver_m['nome']} fora do ar; inicie o servidor e use 'Retomar'") from e
            ctx.log(f"imagem {img['id']}: {e}")
            falhas += 1
            continue
        except Cancelada:
            raise
        except Exception as e:
            ctx.log(f"imagem {img['id']}: {type(e).__name__}: {e}")
            falhas += 1
            continue
        _gravar(conn, p, img, tentativas, melhor, (det_m["nome"], ver_m["nome"]))
        conn.commit()  # por imagem: cancelar ou cair não perde o que já foi feito
        feitas += 1
        contagem[melhor["decisao"]] += 1
        ctx.log(f"imagem {img['id']}: {melhor['decisao']} após {len(tentativas)} tentativa(s), "
                f"{len(melhor['aceitas'])} caixa(s)" + (f" ({melhor['problema']})" if melhor["problema"] else ""))
    ctx.progresso(feitas, total, "Concluído")
    return {"mensagem": f"{contagem['aceita']} aceita(s) pelos critérios, {contagem['revisar']} para revisar, "
                        f"{contagem['rejeitada']} rejeitada(s)" + (f", {falhas} com erro (ficam pendentes)" if falhas else ""),
            **contagem, "falhas": falhas}


def resumo(conn, prerotulagem_id):
    """Contagens e a avaliação pela amostra humana (só imagens da amostra já aprovadas por uma pessoa)."""
    r = conn.execute("""SELECT count(*) AS feitas, count(*) FILTER (WHERE decisao = 'aceita') AS aceitas,
                               count(*) FILTER (WHERE decisao = 'revisar') AS revisar,
                               count(*) FILTER (WHERE decisao = 'rejeitada') AS rejeitadas,
                               coalesce(avg(tentativas), 0)::float AS tentativas_media
                        FROM prerotulo WHERE prerotulagem_id = %s""", (prerotulagem_id,)).fetchone()
    amostra = conn.execute("""SELECT r.decisao, r.caixas, i.estado, i.aprovacao,
                                     coalesce(jsonb_agg(a.bbox) FILTER (WHERE a.classe = 'fachada'), '[]') AS humano
                              FROM prerotulagem p JOIN dataset_imagem i ON i.id = ANY (p.imagem_ids) AND i.amostra_avaliacao
                              LEFT JOIN prerotulo r ON r.prerotulagem_id = p.id AND r.imagem_id = i.id
                              LEFT JOIN anotacao a ON a.imagem_id = i.id
                              WHERE p.id = %s GROUP BY i.id, r.decisao, r.caixas""", (prerotulagem_id,)).fetchall()
    revisadas = [x for x in amostra if x["decisao"] and x["estado"] == "aprovada" and x["aprovacao"] == "humana"]
    acertos = falsos = omitidas = aceitas_ok = aceitas = 0
    for x in revisadas:
        a, f, o = comparar(x["caixas"], x["humano"])
        acertos, falsos, omitidas = acertos + a, falsos + f, omitidas + o
        if x["decisao"] == "aceita":
            aceitas += 1
            aceitas_ok += not f and not o
    return {**r, "amostra": len(amostra), "amostra_revisada": len(revisadas),
            "precisao": acertos / (acertos + falsos) if revisadas and acertos + falsos else None,
            "revocacao": acertos / (acertos + omitidas) if revisadas and acertos + omitidas else None,
            "aceitas_na_amostra": aceitas, "aceitas_sem_correcao": aceitas_ok}
