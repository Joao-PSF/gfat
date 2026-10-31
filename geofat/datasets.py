"""Datasets de anotação para treinar detectores: imagens (fotos do GeoFAT ou enviadas), caixas por classe,
revisão/aprovação, divisão treino/validação/teste e exportação no formato YOLO."""
import io
import shutil
import uuid

from PIL import Image, ImageOps
from psycopg.types.json import Jsonb

from .config import FOTOS_DIR, RAIZ
from .db import registrar
from .fotos import abrir
from .yolo_dataset import escrever_data_yaml, linhas_yolo

CONJUNTOS = {"treino": "train", "validacao": "val", "teste": "test"}
# Texto de busca padrão para detectores por descrição (Grounding DINO entende melhor em inglês)
TEXTOS_PADRAO = {"fachada": "building facade", "letreiro": "shop sign", "porta_enrolar": "roll-up door",
                 "porta": "door", "janela": "window", "portao": "gate", "hidrometro": "water meter",
                 "numero_predial": "house number", "terreno_vago": "vacant lot", "placa": "sign board"}
MAX_UPLOAD = 40 * 1024 * 1024


# Divisão padrão 70/20/10 na ordem de entrada; o revisor pode trocar imagem por imagem
DIVISAO = ("treino",) * 7 + ("validacao",) * 2 + ("teste",)


def _conjunto(n):
    return DIVISAO[n % 10]


def _proximo(conn, dataset_id):
    return conn.execute("SELECT count(*) AS n FROM dataset_imagem WHERE dataset_id = %s", (dataset_id,)).fetchone()["n"]


def criar(conn, nome, classes, origem="vazio"):
    """origem: 'vazio', 'fotos' (todas as fotos do GeoFAT, sem caixas) ou 'revisadas' (fotos com as fachadas já
    revisadas na Revisão de imagens, que entram como caixas da classe 'fachada')."""
    ds = conn.execute("INSERT INTO dataset (nome, classes, textos_busca) VALUES (%s, %s, %s) RETURNING id",
                      (nome, classes, Jsonb({c: TEXTOS_PADRAO.get(c, c.replace("_", " ")) for c in classes}))
                      ).fetchone()["id"]
    if origem == "fotos":
        adicionar_fotos(conn, ds, [r["id"] for r in conn.execute("SELECT id FROM foto ORDER BY id")])
    elif origem == "revisadas":
        fotos = conn.execute("""SELECT f.id, coalesce(array_agg(i.bbox) FILTER (WHERE i.fachada_valida), '{}') AS caixas
                                FROM foto f JOIN imovel i ON i.foto_id = f.id GROUP BY f.id
                                HAVING bool_and(i.revisado_em IS NOT NULL) AND bool_and(i.bbox IS NOT NULL)
                                ORDER BY f.id""").fetchall()
        ids = adicionar_fotos(conn, ds, [f["id"] for f in fotos])
        # Só 'fachada' está anotada; com outras classes no dataset a imagem ainda precisa de revisão
        estado = "aprovada" if classes == ["fachada"] else "revisar"
        for img_id, f in zip(ids, fotos):
            if "fachada" in classes:
                for b in f["caixas"]:
                    conn.execute("INSERT INTO anotacao (imagem_id, classe, bbox, origem) VALUES (%s, 'fachada', %s, 'aceita')",
                                 (img_id, b))
            final = estado if f["caixas"] or estado == "aprovada" else "vazia"
            conn.execute("UPDATE dataset_imagem SET estado = %s, aprovacao = %s WHERE id = %s",
                         (final, "humana" if final == "aprovada" else None, img_id))
    registrar(conn, "dataset", ds, "criado", {"nome": nome, "classes": classes, "origem": origem})
    return ds


def adicionar_fotos(conn, dataset_id, foto_ids):
    """Fotos já cadastradas no GeoFAT. Retorna os ids das imagens criadas (as já presentes são ignoradas)."""
    n, ids = _proximo(conn, dataset_id), []
    for f in conn.execute("SELECT id, arquivo, largura, altura FROM foto WHERE id = ANY (%s) ORDER BY id", (foto_ids,)):
        r = conn.execute("""INSERT INTO dataset_imagem (dataset_id, foto_id, arquivo, nome, largura, altura, conjunto)
                            VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id""",
                         (dataset_id, f["id"], f["arquivo"], f["arquivo"].rsplit("/", 1)[-1], f["largura"], f["altura"],
                          _conjunto(n))).fetchone()
        if r:
            ids.append(r["id"])
            n += 1
    return ids


def adicionar_upload(conn, dataset_id, nome, dados):
    """Imagem enviada pelo navegador. É recodificada em JPEG, com a rotação do EXIF aplicada e sem metadados
    (o GPS de uma foto de celular não fica guardado no dataset)."""
    if len(dados) > MAX_UPLOAD:
        raise ValueError("imagem maior que 40 MB")
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(dados))).convert("RGB")
    except Exception as e:
        raise ValueError(f"{nome}: não é uma imagem válida") from e
    relativo = f"datasets/{dataset_id}/{uuid.uuid4().hex}.jpg"
    (FOTOS_DIR / relativo).parent.mkdir(parents=True, exist_ok=True)
    img.save(FOTOS_DIR / relativo, "JPEG", quality=95)
    return conn.execute("""INSERT INTO dataset_imagem (dataset_id, arquivo, nome, largura, altura, conjunto)
                           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                        (dataset_id, relativo, nome[:200], img.width, img.height,
                         _conjunto(_proximo(conn, dataset_id)))).fetchone()["id"]


def salvar_anotacoes(conn, imagem_id, anotacoes, aprovar=False):
    """Substitui as caixas da imagem. Aprovada = revisada e pronta para o treino (sem caixas = imagem de fundo)."""
    from .prerotulagem import versionar

    img = conn.execute("""SELECT i.*, d.classes FROM dataset_imagem i JOIN dataset d ON d.id = i.dataset_id
                          WHERE i.id = %s FOR UPDATE""", (imagem_id,)).fetchone()
    if not img:
        raise LookupError("imagem não encontrada")
    for a in anotacoes:
        x1, y1, x2, y2 = a["bbox"]
        if a["classe"] not in img["classes"]:
            raise ValueError(f"classe '{a['classe']}' não existe no dataset")
        if not (0 <= x1 < x2 <= img["largura"] and 0 <= y1 < y2 <= img["altura"]):
            raise ValueError("caixa fora da imagem")
    if aprovar and any(a["origem"] == "sugestao" for a in anotacoes):
        raise ValueError("aceite ou descarte as sugestões antes de aprovar")
    conn.execute("DELETE FROM anotacao WHERE imagem_id = %s", (imagem_id,))
    for a in anotacoes:
        conn.execute("INSERT INTO anotacao (imagem_id, classe, bbox, origem, score, modelo) VALUES (%s, %s, %s, %s, %s, %s)",
                     (imagem_id, a["classe"], a["bbox"], a["origem"], a.get("score"), a.get("modelo")))
    estado = "aprovada" if aprovar else ("revisar" if anotacoes else "vazia")
    conn.execute("UPDATE dataset_imagem SET estado = %s, aprovacao = %s, atualizada_em = now() WHERE id = %s",
                 (estado, "humana" if aprovar else None, imagem_id))
    versionar(conn, imagem_id, "manual")
    registrar(conn, "dataset_imagem", imagem_id, "aprovada" if aprovar else "anotacoes",
              {"caixas": len(anotacoes), "sugestoes": sum(a["origem"] == "sugestao" for a in anotacoes)})
    return estado


def sugerir(conn, ctx, dataset_id, imagem_ids, modelo_id, limiar=None):
    """Pré-anotação com o detector escolhido. As caixas entram como 'sugestão' e precisam ser aceitas."""
    from .prerotulagem import versionar
    from .provedores import TIPOS, modelo_por_id, obter_detector

    m = modelo_por_id(conn, modelo_id)
    if "anotacao" not in TIPOS[m["tipo"]]["etapas"]:
        raise ValueError(f"{m['nome']} não sugere caixas")
    ds = conn.execute("SELECT classes, textos_busca FROM dataset WHERE id = %s", (dataset_id,)).fetchone()
    classes = {c: ds["textos_busca"].get(c) or c.replace("_", " ") for c in ds["classes"]}
    imagens = conn.execute("SELECT id, arquivo FROM dataset_imagem WHERE dataset_id = %s AND id = ANY (%s) ORDER BY id",
                           (dataset_id, imagem_ids)).fetchall()
    ctx.progresso(0, len(imagens), f"Carregando {m['nome']}", "imagens")
    detector = obter_detector(m)
    total = 0
    for n, img in enumerate(imagens):
        ctx.verificar()
        ctx.progresso(n, len(imagens), f"Detectando na imagem {n + 1} de {len(imagens)}")
        caixas = detector(abrir(FOTOS_DIR / img["arquivo"]), classes, limiar)
        conn.execute("DELETE FROM anotacao WHERE imagem_id = %s AND origem = 'sugestao'", (img["id"],))
        for bbox, score, classe in caixas:
            if bbox[2] - bbox[0] >= 2 and bbox[3] - bbox[1] >= 2:
                conn.execute("""INSERT INTO anotacao (imagem_id, classe, bbox, origem, score, modelo)
                                VALUES (%s, %s, %s, 'sugestao', %s, %s)""", (img["id"], classe, bbox, score, m["nome"]))
        conn.execute("UPDATE dataset_imagem SET estado = 'revisar', aprovacao = NULL, atualizada_em = now() WHERE id = %s",
                     (img["id"],))
        versionar(conn, img["id"], "sugestao")
        conn.commit()
        total += len(caixas)
        ctx.log(f"{img['id']}: {len(caixas)} sugestão(ões)")
    ctx.progresso(len(imagens), len(imagens), "Concluído")
    return {"mensagem": f"{total} caixa(s) sugerida(s) em {len(imagens)} imagem(ns) por {m['nome']}", "caixas": total}


def exportar(conn, dataset_id, saida, ctx=None):
    """Imagens aprovadas -> images/ e labels/ por conjunto + data.yaml. Retorna quantas imagens por conjunto."""
    ds = conn.execute("SELECT * FROM dataset WHERE id = %s", (dataset_id,)).fetchone()
    imagens = conn.execute("""SELECT i.id, i.arquivo, i.largura, i.altura, i.conjunto,
                                     coalesce(array_agg(a.bbox ORDER BY a.id) FILTER (WHERE a.id IS NOT NULL), '{}') AS caixas,
                                     coalesce(array_agg(a.classe ORDER BY a.id) FILTER (WHERE a.id IS NOT NULL), '{}') AS classes
                              FROM dataset_imagem i LEFT JOIN anotacao a ON a.imagem_id = i.id
                              WHERE i.dataset_id = %s AND i.estado = 'aprovada'
                              GROUP BY i.id ORDER BY i.id""", (dataset_id,)).fetchall()
    contagem = {c: sum(i["conjunto"] == c for i in imagens) for c in CONJUNTOS}
    if not contagem["treino"] or not contagem["validacao"]:
        raise ValueError(f"o treino precisa de imagens aprovadas no treino e na validação (hoje: {contagem['treino']} "
                         f"no treino, {contagem['validacao']} na validação)")
    if saida.exists():
        shutil.rmtree(saida)
    for n, i in enumerate(imagens):
        if ctx:
            ctx.verificar()
            ctx.progresso(n, len(imagens), "Gerando images/ e labels/", "imagens")
        parte = CONJUNTOS[i["conjunto"]]
        for pasta in ("images", "labels"):
            (saida / pasta / parte).mkdir(parents=True, exist_ok=True)
        abrir(FOTOS_DIR / i["arquivo"]).convert("RGB").save(saida / "images" / parte / f"{i['id']}.jpg", quality=95)
        linhas = linhas_yolo(i["caixas"], i["largura"], i["altura"], [ds["classes"].index(c) for c in i["classes"]])
        (saida / "labels" / parte / f"{i['id']}.txt").write_text("\n".join(linhas))
    escrever_data_yaml(saida, ds["classes"], com_teste=contagem["teste"] > 0)
    escrever_atribuicao(conn, [i["id"] for i in imagens], saida)
    return contagem


def escrever_atribuicao(conn, imagem_ids, saida):
    """CC BY-SA exige atribuição: lista autor, licença e origem das imagens externas que entraram no dataset."""
    linhas = conn.execute("""SELECT i.id, p.fonte, p.id_externo, p.autor, p.licenca, p.termos_url, p.url_pagina
                             FROM dataset_imagem i JOIN foto_procedencia p ON p.foto_id = i.foto_id
                             WHERE i.id = ANY (%s) ORDER BY i.id""", (imagem_ids,)).fetchall()
    if linhas:
        (saida / "ATRIBUICAO.txt").write_text(
            "Imagens de terceiros neste dataset (arquivo = id da imagem no GeoFAT). Obras derivadas: mesma licença.\n\n"
            + "".join(f"{x['id']}.jpg\t{x['fonte']} {x['id_externo']}\t© {x['autor'] or 'autor não informado'}\t"
                      f"{x['licenca']}\t{x['url_pagina']}\t{x['termos_url']}\n" for x in linhas), encoding="utf-8")


def exportar_zip(conn, ctx, dataset_id):
    nome = conn.execute("SELECT nome FROM dataset WHERE id = %s", (dataset_id,)).fetchone()["nome"]
    saida = RAIZ / "data" / "exportacoes" / f"{nome}_yolo"
    contagem = exportar(conn, dataset_id, saida, ctx)
    ctx.progresso(1, 1, "Compactando")
    zip_ = shutil.make_archive(str(saida), "zip", saida)
    return {"mensagem": f"{sum(contagem.values())} imagem(ns) aprovada(s): {contagem['treino']} treino, "
                        f"{contagem['validacao']} validação, {contagem['teste']} teste",
            "arquivo": f"exportacoes/{nome}_yolo.zip", **contagem}
