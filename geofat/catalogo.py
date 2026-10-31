"""Catálogo de modelos locais que o GeoFAT sabe instalar, com download retomável e conferência do SHA-256."""
import hashlib
import json
import urllib.request

from psycopg.types.json import Jsonb

from .config import RAIZ
from .db import conectar

MODELOS_DIR = RAIZ / "modelos"
HF = "https://huggingface.co"

# Arquivos e tamanhos conferidos nos repositórios oficiais em 10/2026.
CATALOGO = {
    "bonsai-2-9b": {"nome": "Bonsai 2 9B (Q2_0)", "etapa": "Classificação", "repo": "prism-ml/Ternary-Bonsai-2-9B-gguf",
                    "arquivos": ["Ternary-Bonsai-2-9B-Q2_0.gguf", "Ternary-Bonsai-2-9B-mmproj-Q8_0.gguf"],
                    "descricao": "Modelo de visão usado na classificação. Roda no llama-server local.", "tamanho": "3,2 GB"},
    "bonsai-2-4b": {"nome": "Bonsai 2 4B (Q2_0)", "etapa": "Classificação", "repo": "prism-ml/Ternary-Bonsai-2-4B-gguf",
                    "arquivos": ["Ternary-Bonsai-2-4B-Q2_0.gguf", "Ternary-Bonsai-2-4B-mmproj-Q8_0.gguf"],
                    "descricao": "Versão menor e mais rápida do Bonsai 2, sem avaliação nas fachadas do GeoFAT.",
                    "tamanho": "1,6 GB"},
    "dino-tiny": {"nome": "Grounding DINO tiny", "etapa": "Detecção", "repo": "IDEA-Research/grounding-dino-tiny",
                  "pasta": "grounding-dino-tiny", "ignorar": ["pytorch_model.bin", ".gitattributes", "README.md"],
                  "descricao": "Detector por descrição em texto. Já usado pelo GeoFAT a partir do cache do Hugging Face.",
                  "tamanho": "0,7 GB"},
    "dino-base": {"nome": "Grounding DINO base", "etapa": "Detecção", "repo": "IDEA-Research/grounding-dino-base",
                  "pasta": "grounding-dino-base", "ignorar": ["pytorch_model.bin", ".gitattributes", "README.md"],
                  "descricao": "Versão maior do Grounding DINO: mais lenta no CPU, sem avaliação no GeoFAT.",
                  "tamanho": "0,9 GB"},
    **{f"yolo11{t}": {"nome": f"YOLO11 {n}", "etapa": "Base para treino",
                      "url": f"https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11{t}.pt",
                      "arquivos": [f"yolo11{t}.pt"], "descricao": d, "tamanho": tam}
       for t, n, d, tam in [("n", "nano", "Base leve para treinar detectores no CPU.", "5 MB"),
                            ("s", "small", "Base intermediária; treino mais lento no CPU.", "19 MB"),
                            ("m", "medium", "Base para mais precisão; treino lento sem GPU.", "39 MB")]},
}


def _destino(item):
    return MODELOS_DIR / item["pasta"] if "pasta" in item else MODELOS_DIR


def instalado(chave):
    item = CATALOGO[chave]
    destino = _destino(item)
    if "pasta" in item:
        return (destino / "config.json").exists() and (destino / "model.safetensors").exists()
    return all((destino / a).exists() for a in item["arquivos"])


def _arquivos_hf(repo, item, token):
    """Lista do repositório no Hugging Face: caminho, tamanho e SHA-256 (dos arquivos grandes)."""
    req = urllib.request.Request(f"{HF}/api/models/{repo}/tree/main",
                                 headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req, timeout=30) as r:
        arvore = [f for f in json.load(r) if f["type"] == "file"]
    if "arquivos" in item:
        arvore = [f for f in arvore if f["path"] in item["arquivos"]]
    else:
        arvore = [f for f in arvore if f["path"] not in item["ignorar"]]
    return [(f"{HF}/{repo}/resolve/main/{f['path']}", f["path"], f.get("size"), (f.get("lfs") or {}).get("oid"))
            for f in arvore]


def _baixar(ctx, url, destino, feito_antes, total, sha256=None, token=None):
    """Download com retomada: o .part fica no disco se cancelar ou cair, e a nova tentativa continua dele."""
    parte = destino.with_name(destino.name + ".part")
    feito = parte.stat().st_size if parte.exists() else 0
    cab = {"Range": f"bytes={feito}-"} if feito else {}
    if token and url.startswith(HF):
        cab["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=cab), timeout=60) as r:
        if feito and r.status != 206:  # servidor ignorou o Range: recomeça
            feito = 0
        with open(parte, "ab" if feito else "wb") as f:
            while bloco := r.read(1 << 20):
                f.write(bloco)
                feito += len(bloco)
                ctx.progresso(feito_antes + feito, total, f"Baixando {destino.name}", "bytes")
                ctx.verificar()
    if sha256:
        ctx.log(f"Conferindo SHA-256 de {destino.name}")
        h = hashlib.sha256()
        with open(parte, "rb") as f:
            while bloco := f.read(1 << 22):
                h.update(bloco)
                ctx.verificar()
        if h.hexdigest() != sha256:
            parte.unlink()
            raise ValueError(f"{destino.name}: SHA-256 não confere, arquivo descartado")
    parte.replace(destino)
    return feito


def instalar(ctx, chave):
    item = CATALOGO[chave]
    with conectar() as conn:
        token = conn.execute("SELECT chave FROM provedor WHERE tipo = 'huggingface' LIMIT 1").fetchone()
    token = token and token["chave"]
    destino = _destino(item)
    destino.mkdir(parents=True, exist_ok=True)
    if "repo" in item:
        arquivos = _arquivos_hf(item["repo"], item, token)
    else:
        with urllib.request.urlopen(urllib.request.Request(item["url"], method="HEAD"), timeout=30) as r:
            arquivos = [(item["url"], item["arquivos"][0], int(r.headers.get("Content-Length") or 0) or None, None)]
    total = sum(a[2] or 0 for a in arquivos) or None
    feito = 0
    for url, caminho, tamanho, sha in arquivos:
        alvo = destino / caminho
        if alvo.exists() and (tamanho is None or alvo.stat().st_size == tamanho):
            ctx.log(f"{caminho}: já instalado")
            feito += tamanho or 0
            continue
        alvo.parent.mkdir(parents=True, exist_ok=True)
        ctx.log(f"Baixando {caminho}")
        feito += _baixar(ctx, url, alvo, feito, total, sha, token)
    ctx.progresso(total or feito, total or feito, "Concluído", "bytes")
    with conectar() as conn:
        _registrar_modelo(conn, chave, item)
    return {"mensagem": f"{item['nome']} instalado em {destino.relative_to(RAIZ).as_posix()}"}


def _registrar_modelo(conn, chave, item):
    """Modelo baixado vira uma opção nas etapas. O GGUF precisa do servidor local rodando com ele (Conexões)."""
    if chave.startswith("bonsai"):
        from .classificar import PARAMETROS_BONSAI

        conn.execute("""INSERT INTO modelo (nome, tipo, provedor_id, config)
                        SELECT %s, 'vlm', id, %s FROM provedor WHERE nome = 'Bonsai local (llama-server)'
                        ON CONFLICT (nome) DO NOTHING""",
                     (item["nome"].split(" (")[0], Jsonb({"modelo": item["arquivos"][0].removesuffix(".gguf"),
                                                          "parametros": PARAMETROS_BONSAI})))
    if chave.startswith("dino"):
        conn.execute("""INSERT INTO modelo (nome, tipo, config) VALUES (%s, 'grounding_dino', %s)
                        ON CONFLICT (nome) DO NOTHING""",
                     (f"{item['nome']} (local)", Jsonb({"modelo": f"modelos/{item['pasta']}", "limiar": 0.35,
                                                        "prompt": "building facade."})))
