"""Detecção de fachadas: Grounding DINO (sem treino) ou YOLO (treinado com as revisões).

Todo detector é uma função detector(img, classes=None, limiar=None) -> [(bbox, score, classe)], com
bbox x1,y1,x2,y2 em pixels dentro da imagem. `classes` é {classe: texto de busca}; sem ela, só fachadas."""
from pathlib import Path

from .config import FOTOS_DIR
from .fotos import abrir
from .tarefas import ContextoConsole

PROMPT_FACHADA = "building facade."


def _recortar_na_imagem(caixa, img):
    x1, y1, x2, y2 = (round(v) for v in caixa)
    return [max(0, x1), max(0, y1), min(img.width, x2), min(img.height, y2)]


def carregar_dino(modelo="IDEA-Research/grounding-dino-tiny", limiar=0.35, prompt=PROMPT_FACHADA):
    import torch
    from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

    processador = AutoProcessor.from_pretrained(modelo)
    rede = AutoModelForZeroShotObjectDetection.from_pretrained(modelo).eval()
    padrao = limiar

    def detectar(img, classes=None, limiar=None):
        # DINO recebe frases em minúsculas separadas por ponto e devolve qual frase casou com cada caixa
        frases = {t.strip().lower().rstrip(".").strip(): c for c, t in (classes or {"fachada": prompt}).items()}
        entrada = processador(images=img, text=". ".join(frases) + ".", return_tensors="pt")
        with torch.no_grad():
            saida = rede(**entrada)
        r = processador.post_process_grounded_object_detection(
            saida, entrada.input_ids, threshold=limiar or padrao, text_threshold=0.25, target_sizes=[img.size[::-1]])[0]
        unica = next(iter(frases.values())) if len(frases) == 1 else None
        caixas = []
        for caixa, score, rotulo in zip(r["boxes"], r["scores"], r["text_labels"]):
            rotulo = rotulo.strip()
            classe = unica or frases.get(rotulo) or next(
                (c for f, c in frases.items() if rotulo and (rotulo in f or f in rotulo)), None)
            if classe:
                caixas.append((_recortar_na_imagem(caixa.tolist(), img), float(score), classe))
        return caixas

    return detectar


def carregar_yolo(pesos, limiar=0.35):
    from ultralytics import YOLO

    rede = YOLO(pesos)
    padrao = limiar

    def detectar(img, classes=None, limiar=None):
        r = rede.predict(img, conf=limiar or padrao, verbose=False)[0]
        caixas = [(_recortar_na_imagem(c, img), float(s), r.names[int(k)])
                  for c, s, k in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist(), r.boxes.cls.tolist())]
        # Modelo treinado sem nenhuma das classes pedidas (ex.: só 'fachada' com outro nome): devolve tudo
        if classes and set(classes) & set(r.names.values()):
            caixas = [c for c in caixas if c[2] in classes]
        return caixas

    return detectar


def _area(b):
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def _dentro(menor, maior):
    """Fração da caixa `menor` coberta por `maior`."""
    inter = [max(menor[0], maior[0]), max(menor[1], maior[1]), min(menor[2], maior[2]), min(menor[3], maior[3])]
    return _area(inter) / (_area(menor) or 1)


def filtrar_caixas(caixas):
    """Remove caixas que agrupam 2+ fachadas detectadas e duplicatas quase idênticas (fica a de maior score).
    Cada item é (bbox, score, ...); os demais campos são preservados."""
    caixas = sorted(caixas, key=lambda c: -c[1])
    grupos = {k for k, a in enumerate(caixas)
              if sum(_dentro(b[0], a[0]) > 0.9 for j, b in enumerate(caixas) if j != k) >= 2}
    mantidas = []
    for k, c in enumerate(caixas):
        if k not in grupos and not any(_dentro(c[0], m[0]) > 0.9 and _dentro(m[0], c[0]) > 0.9 for m in mantidas):
            mantidas.append(c)
    return mantidas


def detectar(conn, detector, nome, limite=None, ctx=None):
    """Cria um imóvel por fachada em cada foto ainda não processada. Sem detecção, usa a foto inteira."""
    ctx = ctx or ContextoConsole()
    fotos = conn.execute("SELECT id, arquivo FROM foto WHERE NOT detectada ORDER BY id LIMIT %s",
                         (limite,)).fetchall()
    total = 0
    ctx.progresso(0, len(fotos), unidade="fotos")
    for n, f in enumerate(fotos, 1):
        ctx.verificar()
        caixas = filtrar_caixas(detector(abrir(FOTOS_DIR / f["arquivo"]))) or [(None, None)]
        for bbox, score, *_ in caixas:
            conn.execute("""INSERT INTO imovel (foto_id, bbox, bbox_detectada, detector, score_deteccao, geom)
                            SELECT id, %s, %s, %s, %s, geom FROM foto WHERE id = %s""",
                         (bbox, bbox, nome, score, f["id"]))
        conn.execute("UPDATE foto SET detectada = true WHERE id = %s", (f["id"],))
        conn.commit()  # por foto: se cair no meio, o que já foi feito fica salvo
        total += len(caixas)
        ctx.log(f"{Path(f['arquivo']).name}: {len(caixas)} fachada(s)")
        ctx.progresso(n, len(fotos))
    if fotos:
        from .localizar import localizar

        localizar(conn)  # fachadas novas: estima a posição pela direção da câmera
    return len(fotos), total
