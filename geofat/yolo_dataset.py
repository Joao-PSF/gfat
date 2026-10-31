"""Exporta as fachadas revisadas no formato de treino do YOLO (Ultralytics)."""
from .config import FOTOS_DIR
from .fotos import abrir


def linhas_yolo(caixas, largura, altura, indices=None):
    """Caixas x1,y1,x2,y2 em pixels -> linhas 'classe cx cy w h' normalizadas (classe 0 se não houver índices)."""
    indices = indices or [0] * len(caixas)
    return [f"{k} {(x1 + x2) / 2 / largura:.6f} {(y1 + y2) / 2 / altura:.6f} "
            f"{(x2 - x1) / largura:.6f} {(y2 - y1) / altura:.6f}" for k, (x1, y1, x2, y2) in zip(indices, caixas)]


def escrever_data_yaml(saida, nomes, com_teste=False):
    lista = "".join(f"  {i}: {n}\n" for i, n in enumerate(nomes))
    (saida / "data.yaml").write_text(f"path: {saida.resolve().as_posix()}\ntrain: images/train\nval: images/val\n"
                                     + ("test: images/test\n" if com_teste else "") + f"names:\n{lista}", encoding="utf-8")


def exportar_yolo(conn, saida, raiz=FOTOS_DIR):
    # Só fotos com todas as caixas revisadas: caixa não revisada viraria "fundo" no treino.
    fotos = conn.execute("""
        SELECT f.id, f.arquivo, f.largura, f.altura,
               coalesce(array_agg(i.bbox) FILTER (WHERE i.fachada_valida), '{}') AS caixas
        FROM foto f JOIN imovel i ON i.foto_id = f.id
        GROUP BY f.id
        HAVING bool_and(i.fachada_valida IS NOT NULL) AND bool_and(i.bbox IS NOT NULL)""").fetchall()
    for f in fotos:
        parte = "val" if f["id"] % 5 == 0 else "train"
        for pasta in ("images", "labels"):
            (saida / pasta / parte).mkdir(parents=True, exist_ok=True)
        abrir(raiz / f["arquivo"]).save(saida / "images" / parte / f"{f['id']}.jpg", quality=95)
        texto = "\n".join(linhas_yolo(f["caixas"], f["largura"], f["altura"]))
        (saida / "labels" / parte / f"{f['id']}.txt").write_text(texto)
    escrever_data_yaml(saida, ["fachada"])
    return len(fotos)
