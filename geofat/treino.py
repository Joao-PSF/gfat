"""Treino experimental de detectores YOLO (Ultralytics, CPU) com um dataset do GeoFAT. As métricas por época
vêm do próprio Ultralytics, medidas no conjunto de validação."""
import time

from psycopg.types.json import Jsonb

from .config import RAIZ
from .db import conectar

PASTA_RUNS = RAIZ / "runs" / "geofat"


def bases_disponiveis(conn):
    """Pesos de partida: bases YOLO baixadas pelo catálogo e versões já treinadas."""
    from .catalogo import CATALOGO

    bases = [{"valor": f"modelos/{p.name}", "nome": CATALOGO.get(p.stem, {}).get("nome", p.stem)}
             for p in sorted((RAIZ / "modelos").glob("yolo*.pt"))]
    for t in conn.execute("SELECT nome, pesos FROM treino WHERE pesos IS NOT NULL ORDER BY id DESC"):
        if (RAIZ / t["pesos"]).exists():
            bases.append({"valor": t["pesos"], "nome": f"Continuar de {t['nome']}"})
    return bases


def criar(conn, dataset_id, base, epocas, imgsz=640, lote=8):
    if base not in {b["valor"] for b in bases_disponiveis(conn)}:
        raise ValueError("modelo base não instalado (página Modelos > Catálogo)")
    ds = conn.execute("SELECT nome, classes FROM dataset WHERE id = %s", (dataset_id,)).fetchone()
    if not ds:
        raise LookupError("dataset não encontrado")
    n = conn.execute("SELECT count(*) AS n FROM treino WHERE dataset_id = %s", (dataset_id,)).fetchone()["n"]
    # Imagens externas só entram no treino com a licença registrada na importação
    sem_licenca = conn.execute("""SELECT count(*) AS n FROM dataset_imagem i JOIN foto f ON f.id = i.foto_id
                                  LEFT JOIN foto_procedencia p ON p.foto_id = f.id
                                  WHERE i.dataset_id = %s AND i.estado = 'aprovada' AND f.fonte = 'mapillary'
                                    AND p.foto_id IS NULL""", (dataset_id,)).fetchone()["n"]
    if sem_licenca:
        raise ValueError(f"{sem_licenca} imagem(ns) do Mapillary sem licença registrada; importe-as de novo pelo painel")
    licencas = {r["licenca"]: r["n"] for r in conn.execute(
        """SELECT p.licenca, count(*) AS n FROM dataset_imagem i JOIN foto_procedencia p ON p.foto_id = i.foto_id
           WHERE i.dataset_id = %s AND i.estado = 'aprovada' GROUP BY p.licenca""", (dataset_id,))}
    parametros = {"base": base, "epocas": epocas, "imgsz": imgsz, "lote": lote, "dispositivo": "cpu", "workers": 0,
                  "licencas_externas": licencas}
    return conn.execute("""INSERT INTO treino (nome, dataset_id, parametros, classes) VALUES (%s, %s, %s, %s)
                           RETURNING id, nome""", (f"{ds['nome']}-v{n + 1}", dataset_id, Jsonb(parametros), ds["classes"])
                        ).fetchone()


def _num(v):
    try:
        return round(float(v), 5)
    except (TypeError, ValueError):
        return None


def treinar(ctx, treino_id):
    from ultralytics import YOLO

    from .datasets import exportar

    with conectar() as conn:
        t = conn.execute("SELECT * FROM treino WHERE id = %s", (treino_id,)).fetchone()
        conn.execute("UPDATE treino SET tarefa_id = %s, metricas = '[]' WHERE id = %s", (ctx.id, treino_id))
        ctx.log(f"Exportando o dataset (só imagens aprovadas)")
        pasta = PASTA_RUNS / t["nome"] / "dataset"
        contagem = exportar(conn, t["dataset_id"], pasta, ctx)
        conn.execute("UPDATE treino SET imagens = %s WHERE id = %s", (Jsonb(contagem), treino_id))
    p = t["parametros"]
    ctx.log(f"{contagem['treino']} imagens de treino, {contagem['validacao']} de validação; base {p['base']}")
    modelo = YOLO(str(RAIZ / p["base"]))
    estado = {"lotes": 0, "por_epoca": 1, "inicio": time.time()}

    def inicio_treino(trainer):
        estado["por_epoca"] = len(trainer.train_loader)
        ctx.progresso(0, p["epocas"] * estado["por_epoca"], "Época 1", "lotes")

    def fim_lote(trainer):
        estado["lotes"] += 1
        ctx.progresso(estado["lotes"], None, f"Época {trainer.epoch + 1} de {p['epocas']}")
        ctx.verificar()  # cancelamento entre lotes

    def fim_epoca(trainer):
        if trainer.epoch >= p["epocas"]:  # o Ultralytics chama de novo na validação final do best.pt
            return
        m = {"epoca": trainer.epoch + 1, "segundos": round(time.time() - estado["inicio"], 1)}
        m.update({k: _num(v) for k, v in trainer.metrics.items()})
        m.update({k: _num(v) for k, v in trainer.label_loss_items(trainer.tloss, prefix="train").items()})
        with conectar() as conn:
            conn.execute("UPDATE treino SET metricas = metricas || %s WHERE id = %s", (Jsonb([m]), treino_id))
        ctx.log(f"época {m['epoca']}: mAP50 {m.get('metrics/mAP50(B)')}, precisão {m.get('metrics/precision(B)')}, "
                f"revocação {m.get('metrics/recall(B)')}")

    modelo.add_callback("on_train_start", inicio_treino)
    modelo.add_callback("on_train_batch_end", fim_lote)
    modelo.add_callback("on_fit_epoch_end", fim_epoca)
    modelo.train(data=str(pasta / "data.yaml"), epochs=p["epocas"], imgsz=p["imgsz"], batch=p["lote"],
                 device=p["dispositivo"], workers=p["workers"], project=str(PASTA_RUNS), name=t["nome"], exist_ok=True,
                 plots=False, verbose=False)
    melhor = modelo.trainer.best
    if not melhor.exists():
        raise RuntimeError("o treino terminou sem gerar best.pt")
    with conectar() as conn:
        conn.execute("UPDATE treino SET pesos = %s WHERE id = %s", (melhor.relative_to(RAIZ).as_posix(), treino_id))
    return {"mensagem": f"{t['nome']} treinado: pesos em {melhor.relative_to(RAIZ).as_posix()}"}
