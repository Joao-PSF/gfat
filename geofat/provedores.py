"""Adaptadores de IA: cada etapa do pipeline usa o modelo escolhido na página Modelos, sem mudar o código.

Detector:      detector(img, classes=None, limiar=None) -> [(bbox, score, classe)]   (ver detectar.py)
Classificador: perguntar(jpeg, prompt) -> texto do modelo                          (ver classificar.py)
OCR:           ler(jpeg) -> texto da fachada
"""
import base64
import collections
import io
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from psycopg.types.json import Jsonb

from .config import BONSAI_MODEL, BONSAI_URL, RAIZ

# O que cada tipo de modelo sabe fazer. Nem todo provedor tem as mesmas capacidades.
TIPOS = {
    "grounding_dino": {"nome": "Grounding DINO", "etapas": ["deteccao", "anotacao"], "busca_por_texto": True,
                       "descricao": "Detector local que encontra objetos descritos em texto, sem treino"},
    "yolo": {"nome": "YOLO (Ultralytics)", "etapas": ["deteccao", "anotacao"], "busca_por_texto": False,
             "descricao": "Detector local treinado com as anotações do GeoFAT; só conhece as classes do treino"},
    "roboflow": {"nome": "Roboflow (hospedado)", "etapas": ["deteccao", "anotacao"], "busca_por_texto": False,
                 "provedor": "roboflow", "descricao": "Detector na nuvem da Roboflow; as imagens saem do computador"},
    "vlm": {"nome": "Modelo de visão e linguagem", "etapas": ["classificacao", "ocr"], "provedor": "openai",
            "descricao": "Bonsai, Qwen-VL e outros por API compatível com a da OpenAI (llama-server, Ollama, OpenRouter)"},
    "paddleocr": {"nome": "PaddleOCR", "etapas": ["ocr"],
                  "descricao": "OCR local; requer pip install paddleocr paddlepaddle (não vem instalado)"},
}
ETAPAS = {"deteccao": "Encontrar a fachada", "anotacao": "Sugerir anotações no editor de dataset",
          "ocr": "Ler letreiros", "classificacao": "Classificar o imóvel"}

_cache, _trava = {}, threading.Lock()
# GPU remota (Runpod): o que pode rodar lá, quantas tentativas por chamada e as últimas falhas (painel Modelos)
REMOTOS = ("grounding_dino", "paddleocr")  # e VLM servido localmente (Bonsai), ver _com_ambiente
ESPERAS_REMOTAS = (5, 20)                  # segundos antes da 2ª e da 3ª tentativa
OCORRENCIAS_REMOTAS = collections.deque(maxlen=50)


class RemotoIndisponivel(ValueError):
    pass


def processamento(conn):
    """Ambiente escolhido (local/runpod), custo e orçamento, e a conexão da GPU remota (com a chave, só no servidor)."""
    cfg = conn.execute("SELECT valor FROM configuracao WHERE chave = 'processamento'").fetchone()["valor"]
    p = conn.execute("SELECT id, url, chave FROM provedor WHERE tipo = 'gpu_remota' ORDER BY id LIMIT 1").fetchone()
    return {**cfg, "remoto": p}


def _com_ambiente(conn, m):
    """No ambiente Runpod, os modelos que rodariam neste computador (Grounding DINO, PaddleOCR, VLM de servidor
    local como o Bonsai) passam a rodar na GPU remota. YOLO e serviços de nuvem não mudam."""
    if m and m["tipo"] in REMOTOS + ("vlm",) and (m["tipo"] != "vlm" or m["provedor_local"]):
        cfg = processamento(conn)
        if cfg["ambiente"] == "runpod":
            r = cfg["remoto"] or {}
            m["remoto"] = {"url": (r.get("url") or "").rstrip("/"), "chave": r.get("chave")}
    return m


def semear(conn):
    """Configuração inicial (uma vez por banco): o pipeline que já existia, Bonsai local + Grounding DINO."""
    if conn.execute("SELECT 1 FROM migracao WHERE nome = 'semente-ia'").fetchone():
        return
    from .classificar import PARAMETROS_BONSAI

    for nome, tipo, url, local in [
            ("Bonsai local (llama-server)", "openai", BONSAI_URL, True),
            ("Ollama", "openai", "http://localhost:11434/v1", True),
            ("OpenRouter", "openai", "https://openrouter.ai/api/v1", False),
            ("Roboflow", "roboflow", "https://detect.roboflow.com", False),
            ("Hugging Face", "huggingface", "https://huggingface.co", False)]:
        conn.execute("INSERT INTO provedor (nome, tipo, url, local) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                     (nome, tipo, url, local))
    dino = conn.execute(
        """INSERT INTO modelo (nome, tipo, config) VALUES ('Grounding DINO tiny', 'grounding_dino', %s)
           ON CONFLICT (nome) DO UPDATE SET nome = excluded.nome RETURNING id""",
        (Jsonb({"modelo": "IDEA-Research/grounding-dino-tiny", "limiar": 0.35, "prompt": "building facade."}),)
    ).fetchone()["id"]
    bonsai = conn.execute(
        """INSERT INTO modelo (nome, tipo, provedor_id, config)
           SELECT 'Bonsai 2 9B', 'vlm', id, %s FROM provedor WHERE nome = 'Bonsai local (llama-server)'
           ON CONFLICT (nome) DO UPDATE SET nome = excluded.nome RETURNING id""",
        (Jsonb({"modelo": BONSAI_MODEL, "parametros": PARAMETROS_BONSAI}),)).fetchone()["id"]
    for etapa, modelo_id in [("deteccao", dino), ("anotacao", dino), ("classificacao", bonsai), ("ocr", None)]:
        conn.execute("INSERT INTO etapa (etapa, modelo_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (etapa, modelo_id))
    conn.execute("INSERT INTO migracao (nome) VALUES ('semente-ia')")


SQL_MODELO = """SELECT m.*, p.nome AS provedor_nome, p.tipo AS provedor_tipo, p.url AS provedor_url,
                       p.local AS provedor_local, p.chave AS provedor_chave
                FROM modelo m LEFT JOIN provedor p ON p.id = m.provedor_id"""


def modelo_por_id(conn, modelo_id):
    m = conn.execute(SQL_MODELO + " WHERE m.id = %s", (modelo_id,)).fetchone()
    if not m:
        raise ValueError(f"modelo {modelo_id} não existe")
    return _com_ambiente(conn, m)


def modelo_da_etapa(conn, etapa, obrigatorio=True):
    m = conn.execute(SQL_MODELO + " JOIN etapa e ON e.modelo_id = m.id WHERE e.etapa = %s", (etapa,)).fetchone()
    if not m and obrigatorio:
        raise ValueError(f"nenhum modelo escolhido para a etapa '{ETAPAS[etapa]}' (página Modelos)")
    if m and etapa not in TIPOS[m["tipo"]]["etapas"]:
        raise ValueError(f"o modelo {m['nome']} não faz a etapa '{ETAPAS[etapa]}'")
    return _com_ambiente(conn, m)


def _do_cache(m, criar):
    """Modelos locais são carregados uma vez por processo (o Grounding DINO leva segundos e ~700 MB de RAM)."""
    chave = (m["id"], json.dumps(m["config"], sort_keys=True))
    with _trava:
        if chave not in _cache:
            for k in [k for k in _cache if k[0] == m["id"]]:  # configuração mudou: descarta a versão antiga
                del _cache[k]
            _cache[chave] = criar()
        return _cache[chave]


def _jpeg(img, lado_max=1280):
    img = img.copy()
    img.thumbnail((lado_max, lado_max))
    saida = io.BytesIO()
    img.convert("RGB").save(saida, "JPEG", quality=90)  # sem EXIF: a localização GPS não sai junto
    return saida.getvalue(), img.width


def _roboflow(m):
    if not m["provedor_chave"]:
        raise ValueError("Roboflow sem chave de API (página Modelos > Conexões)")
    url = f"{m['provedor_url'].rstrip('/')}/{m['config']['modelo'].strip('/')}"
    padrao = m["config"].get("limiar", 0.4)

    def detectar(img, classes=None, limiar=None):
        jpeg, largura = _jpeg(img)
        escala = img.width / largura
        consulta = urllib.parse.urlencode({"api_key": m["provedor_chave"], "confidence": round((limiar or padrao) * 100)})
        req = urllib.request.Request(f"{url}?{consulta}", data=base64.b64encode(jpeg),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=120) as r:
            previsoes = json.load(r)["predictions"]
        caixas = [([max(0, round((p["x"] - p["width"] / 2) * escala)), max(0, round((p["y"] - p["height"] / 2) * escala)),
                    min(img.width, round((p["x"] + p["width"] / 2) * escala)), min(img.height, round((p["y"] + p["height"] / 2) * escala))],
                   float(p["confidence"]), p["class"]) for p in previsoes]
        if classes and {c[2] for c in caixas} & set(classes):
            caixas = [c for c in caixas if c[2] in classes]
        return caixas

    return detectar


def com_retentativas(funcao, onde):
    """Chamada à GPU remota com até 3 tentativas: só repete falha de rede, tempo esgotado e HTTP 5xx (pod
    iniciando, proxy sobrecarregado). Token recusado e pedido inválido falham na hora."""
    def chamar(*args, **kwargs):
        for tentativa in range(len(ESPERAS_REMOTAS) + 1):
            ultima = tentativa == len(ESPERAS_REMOTAS)
            try:
                return funcao(*args, **kwargs)
            except urllib.error.HTTPError as e:
                erro, desiste = f"HTTP {e.code}", e.code < 500 or ultima
                OCORRENCIAS_REMOTAS.append({"em": time.time(), "onde": onde, "erro": erro, "tentativa": tentativa + 1, "desistiu": desiste})
                if desiste:
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                erro = str(getattr(e, "reason", e))[:200]
                OCORRENCIAS_REMOTAS.append({"em": time.time(), "onde": onde, "erro": erro, "tentativa": tentativa + 1, "desistiu": ultima})
                if ultima:
                    raise RemotoIndisponivel(f"GPU remota sem resposta após {tentativa + 1} tentativas ({erro}); "
                                     "confira se o pod está ligado ou volte ao ambiente Local") from e
            time.sleep(ESPERAS_REMOTAS[tentativa])
    return chamar


def chamar_remoto(remoto, caminho, corpo=None, timeout=300):
    """POST (ou GET sem corpo) autenticado no serviço GPU. Leva só a imagem e os parâmetros da inferência."""
    if not remoto.get("chave"):
        raise ValueError("ambiente Runpod sem token do serviço GPU (Modelos > Ambiente de processamento)")
    req = urllib.request.Request(remoto["url"] + caminho, data=json.dumps(corpo).encode() if corpo is not None else None,
                                 headers={"Authorization": f"Bearer {remoto['chave']}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _dino_remoto(m):
    from .detectar import PROMPT_FACHADA, _recortar_na_imagem

    c, remoto = m["config"], m["remoto"]
    chamar = com_retentativas(chamar_remoto, "detectar")

    def detectar(img, classes=None, limiar=None):
        jpeg, _ = _jpeg(img, max(img.size))  # resolução original, sem EXIF
        r = chamar(remoto, "/detectar", {"imagem": base64.b64encode(jpeg).decode(), "modelo": c["modelo"],
                                         "classes": classes or {"fachada": c.get("prompt") or PROMPT_FACHADA},
                                         "limiar": limiar or c.get("limiar", 0.35)})
        return [(_recortar_na_imagem(b, img), float(s), k) for b, s, k in r["caixas"]]

    return detectar


def obter_detector(m):
    from .detectar import PROMPT_FACHADA, carregar_dino, carregar_yolo

    c = m["config"]
    if m.get("remoto") and m["tipo"] == "grounding_dino":
        return _dino_remoto(m)
    if m["tipo"] == "grounding_dino":
        return _do_cache(m, lambda: carregar_dino(c["modelo"], c.get("limiar", 0.35), c.get("prompt") or PROMPT_FACHADA))
    if m["tipo"] == "yolo":
        pesos = RAIZ / c["pesos"]
        if not pesos.exists():
            raise ValueError(f"pesos do YOLO não encontrados: {c['pesos']}")
        return _do_cache(m, lambda: carregar_yolo(str(pesos), c.get("limiar", 0.35)))
    if m["tipo"] == "roboflow":
        return _roboflow(m)
    raise ValueError(f"{m['nome']} não é um detector")


def modelos_servidos(url, chave=None, timeout=10):
    """Ids dos modelos que um servidor compatível com a OpenAI está servindo (GET /models)."""
    req = urllib.request.Request(url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {chave}"} if chave else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return [x["id"] for x in json.load(r).get("data", [])]


def _vlm(m, prompt_padrao=None):
    from .classificar import perguntar_vlm

    if m.get("remoto"):  # o serviço GPU repassa /v1 para o llama-server dele (mesma API da OpenAI)
        if not m["remoto"]["chave"]:
            raise ValueError("ambiente Runpod sem token do serviço GPU (Modelos > Ambiente de processamento)")
        m = {**m, "provedor_url": m["remoto"]["url"] + "/v1", "provedor_chave": m["remoto"]["chave"],
             "provedor_nome": "GPU remota (Runpod)", "provedor_local": False}
    if not m["provedor_url"]:
        raise ValueError(f"{m['nome']} sem conexão configurada")
    if not m["provedor_local"] and not m["provedor_chave"]:
        raise ValueError(f"{m['provedor_nome']} sem chave de API (página Modelos > Conexões)")
    modelo = m["config"]["modelo"]
    # Garante que o resultado gravado é do modelo escolhido: o llama-server responde com o que estiver carregado
    try:
        servidos = modelos_servidos(m["provedor_url"], m["provedor_chave"])
    except urllib.error.URLError as e:
        raise ValueError(f"{m['provedor_nome']} não responde em {m['provedor_url']} ({e.reason})") from e
    if servidos and modelo not in servidos:
        raise ValueError(f"{m['provedor_nome']} está servindo {', '.join(servidos[:5])}, não {modelo}")

    def perguntar(jpeg, prompt=None):
        return perguntar_vlm(jpeg, m["provedor_url"], modelo, prompt or prompt_padrao, m["config"].get("parametros"),
                             m["provedor_chave"])

    return com_retentativas(perguntar, "vlm") if m.get("remoto") else perguntar


def obter_classificador(m):
    if m["tipo"] != "vlm":
        raise ValueError(f"{m['nome']} não classifica imóveis")
    return _vlm(m), m["config"]["modelo"]


def obter_verificador(m):
    """VLM que confere as caixas da pré-rotulagem: perguntar(jpeg, prompt) -> texto. Qualquer VLM compatível serve."""
    if m["tipo"] != "vlm":
        raise ValueError(f"{m['nome']} não é um modelo de visão e linguagem")
    return _vlm(m)


def obter_ocr(m):
    from .classificar import PROMPT_OCR

    if m["tipo"] == "vlm":
        perguntar = _vlm(m, PROMPT_OCR)
        return lambda jpeg: perguntar(jpeg, PROMPT_OCR)
    if m["tipo"] == "paddleocr" and m.get("remoto"):
        chamar = com_retentativas(chamar_remoto, "ocr")
        return lambda jpeg: chamar(m["remoto"], "/ocr", {"imagem": base64.b64encode(jpeg).decode(),
                                                         "idioma": m["config"].get("idioma", "pt")})["texto"]
    if m["tipo"] == "paddleocr":
        def criar():
            try:
                from paddleocr import PaddleOCR
            except ImportError as e:
                raise ValueError("PaddleOCR não instalado: pip install paddleocr paddlepaddle") from e
            ocr = PaddleOCR(lang=m["config"].get("idioma", "pt"), use_doc_orientation_classify=False,
                            use_doc_unwarping=False, use_textline_orientation=False)

            def ler(jpeg):
                import numpy as np
                from PIL import Image

                resultado = ocr.predict(np.array(Image.open(io.BytesIO(jpeg)).convert("RGB")))
                return "\n".join(t for r in resultado for t in r["rec_texts"]) or "NENHUM"
            return ler
        return _do_cache(m, criar)
    raise ValueError(f"{m['nome']} não faz OCR")


def disponibilidade(m):
    """Se o modelo pode rodar agora, sem carregar nada pesado. (situação, explicação)"""
    c, t = m["config"], m["tipo"]
    if t == "grounding_dino":
        alvo = c.get("modelo", "")
        if (RAIZ / alvo).is_dir() or Path(alvo).is_dir():
            return "pronto", "arquivos locais"
        from huggingface_hub import try_to_load_from_cache

        em_cache = try_to_load_from_cache(alvo, "model.safetensors")
        return ("pronto", "no cache do Hugging Face") if isinstance(em_cache, str) else \
            ("nao_instalado", "baixa ~700 MB do Hugging Face na primeira detecção")
    if t == "yolo":
        return ("pronto", c["pesos"]) if (RAIZ / c.get("pesos", "-")).exists() else ("nao_instalado", "pesos não encontrados")
    if t == "paddleocr":
        import importlib.util

        return ("pronto", "instalado") if importlib.util.find_spec("paddleocr") else \
            ("nao_instalado", "pip install paddleocr paddlepaddle")
    if not m["provedor_id"]:
        return "sem_conexao", "escolha uma conexão"
    if not m["provedor_local"] and not m["provedor_chave"]:
        return "sem_chave", "falta a chave de API da conexão"
    return "conexao", m["provedor_nome"]


def testar_provedor(p):
    """Teste real da conexão. (ok, mensagem)"""
    cab = {"Authorization": f"Bearer {p['chave']}"} if p["chave"] else {}
    try:
        if p["tipo"] == "mapillary":
            from .mapillary import testar

            return testar(p["chave"])
        if p["tipo"] == "gpu_remota":
            if not p["chave"]:
                return False, "sem token do serviço"
            s = chamar_remoto({"url": p["url"].rstrip("/"), "chave": p["chave"]}, "/status", timeout=15)
            gpu = s.get("gpu") or {}
            return True, (f"{gpu.get('nome', 'sem GPU')} · VRAM livre {gpu.get('vram_livre_gb', '?')} de "
                          f"{gpu.get('vram_total_gb', '?')} GB · {len(s.get('modelos_carregados', []))} modelo(s) carregado(s)")
        if p["tipo"] == "openai":
            ids = modelos_servidos(p["url"], p["chave"])
            return True, f"{len(ids)} modelo(s): {', '.join(ids[:3])}{'…' if len(ids) > 3 else ''}"
        if p["tipo"] == "roboflow":
            if not p["chave"]:
                return False, "sem chave de API"
            with urllib.request.urlopen("https://api.roboflow.com/?" + urllib.parse.urlencode({"api_key": p["chave"]}),
                                        timeout=15) as r:
                return True, f"workspace {json.load(r).get('workspace', '?')}"
        url = "https://huggingface.co/api/whoami-v2" if p["chave"] else "https://huggingface.co/api/models/IDEA-Research/grounding-dino-tiny"
        with urllib.request.urlopen(urllib.request.Request(url, headers=cab), timeout=15) as r:
            dados = json.load(r)
            return True, f"conectado como {dados['name']}" if p["chave"] else "acessível (sem token)"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {'chave inválida' if e.code in (401, 403) else e.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, f"sem resposta: {getattr(e, 'reason', e)}"
