"""Serviço GPU do GeoFAT: Grounding DINO, modelo de visão (Bonsai pelo llama.cpp com CUDA) e OCR opcional,
para rodar num GPU Pod do Runpod ou em qualquer máquina com placa NVIDIA.

Independente do GeoFAT: não acessa o banco, não grava imagens nem resultados em disco e não registra o conteúdo
das requisições. Recebe uma imagem por vez com os parâmetros da inferência e devolve o resultado. Uma fila única
executa um trabalho por vez; com CARGA_SEQUENCIAL=1 só um modelo fica na VRAM (troca quando a etapa muda).

Variáveis de ambiente:
  GEOFAT_GPU_TOKEN   obrigatória (mín. 24 caracteres); o GeoFAT manda "Authorization: Bearer <token>"
  MODELOS_DIR        pasta dos GGUF (padrão /workspace/modelos, o volume persistente do Runpod)
  VLM_GGUF, VLM_MMPROJ  arquivos do modelo de visão dentro de MODELOS_DIR (padrão: Bonsai 2 9B)
  VLM_ALIAS          id do modelo na API /v1 (o mesmo configurado na página Modelos do GeoFAT)
  LLAMA_SERVER       executável do llama-server (padrão /opt/llama/llama-server)
  LLAMA_URL          usar um llama-server já rodando em vez de iniciar um (ex.: http://127.0.0.1:1234)
  CARGA_SEQUENCIAL   1 = descarrega o detector antes do VLM e vice-versa (GPU com pouca VRAM)
  TEMPO_MAX_S        espera máxima de um trabalho na fila + execução (padrão 900)
"""
import base64
import binascii
import collections
import hmac
import io
import json
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from PIL import Image
from pydantic import BaseModel, Field

TOKEN = os.environ.get("GEOFAT_GPU_TOKEN", "")
MODELOS_DIR = Path(os.environ.get("MODELOS_DIR", "/workspace/modelos"))
VLM_GGUF = os.environ.get("VLM_GGUF", "Ternary-Bonsai-2-9B-Q2_0.gguf")
VLM_MMPROJ = os.environ.get("VLM_MMPROJ", "Ternary-Bonsai-2-9B-mmproj-Q8_0.gguf")
VLM_ALIAS = os.environ.get("VLM_ALIAS", "Ternary-Bonsai-2-9B-Q2_0")
LLAMA_SERVER = os.environ.get("LLAMA_SERVER", "/opt/llama/llama-server")
LLAMA_URL = os.environ.get("LLAMA_URL", "").rstrip("/")
LLAMA_PORTA = 8081
CARGA_SEQUENCIAL = os.environ.get("CARGA_SEQUENCIAL", "0") == "1"
TEMPO_MAX_S = float(os.environ.get("TEMPO_MAX_S", "900"))
MAX_IMAGEM = 25 * 1024 * 1024  # bytes em base64

if len(TOKEN) < 24:
    raise SystemExit("defina GEOFAT_GPU_TOKEN com pelo menos 24 caracteres (ex.: python -c \"import secrets; print(secrets.token_urlsafe(32))\")")

app = FastAPI(title="GeoFAT GPU", docs_url=None, redoc_url=None, openapi_url=None)


def autenticar(request: Request):
    recebido = request.headers.get("authorization", "").encode()
    if not hmac.compare_digest(recebido, f"Bearer {TOKEN}".encode()):
        raise HTTPException(401, "token inválido")


# ---------------- Fila: um trabalho por vez ----------------

class Trabalho:
    def __init__(self, tipo, funcao):
        self.id, self.tipo, self.funcao = uuid.uuid4().hex[:12], tipo, funcao
        self.estado, self.erro, self.resultado = "fila", None, None
        self.criado, self.iniciado, self.terminado = time.time(), None, None
        self.pronto = threading.Event()

    def resumo(self):
        agora = time.time()
        return {"id": self.id, "tipo": self.tipo, "estado": self.estado, "erro": self.erro,
                "espera_s": round((self.iniciado or agora) - self.criado, 2),
                "duracao_s": round((self.terminado or agora) - self.iniciado, 2) if self.iniciado else None}


_fila = queue.Queue()
_trabalhos = collections.OrderedDict()  # só metadados, sem imagens; os últimos 200
_contadores = collections.Counter()
_trava = threading.Lock()


def _executor():
    while True:
        t = _fila.get()
        if t.estado == "cancelado":
            continue
        t.estado, t.iniciado = "executando", time.time()
        try:
            t.resultado, t.estado = t.funcao(), "concluido"
        except Exception as e:
            t.estado, t.erro = "falhou", f"{type(e).__name__}: {e}"[:500]
        t.terminado, t.funcao = time.time(), None  # solta a imagem
        _contadores[t.estado] += 1
        t.pronto.set()


threading.Thread(target=_executor, name="gpu-fila", daemon=True).start()


def submeter(tipo, funcao):
    t = Trabalho(tipo, funcao)
    with _trava:
        _trabalhos[t.id] = t
        while len(_trabalhos) > 200:
            _trabalhos.popitem(last=False)
    _fila.put(t)
    if not t.pronto.wait(TEMPO_MAX_S):
        t.estado = "cancelado" if t.estado == "fila" else t.estado
        raise HTTPException(504, f"trabalho {t.id} passou de {TEMPO_MAX_S:.0f} s")
    if t.estado != "concluido":
        raise HTTPException(500, t.erro or "falhou")
    r, t.resultado = t.resultado, None
    return r


# ---------------- Modelos ----------------

_dino = {}                                   # nome -> (processador, rede)
_llama = {"p": None}


def _gpu():
    try:
        import torch

        if torch.cuda.is_available():
            livre, total = torch.cuda.mem_get_info()
            return {"nome": torch.cuda.get_device_name(0), "vram_livre_gb": round(livre / 2**30, 1),
                    "vram_total_gb": round(total / 2**30, 1), "cuda": torch.version.cuda}
    except Exception:
        pass
    return None


def _llama_url():
    return LLAMA_URL or f"http://127.0.0.1:{LLAMA_PORTA}"


def _llama_pronto():
    try:
        with urllib.request.urlopen(_llama_url() + "/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _parar_llama():
    p = _llama["p"]
    if p is not None and p.poll() is None:
        p.terminate()
        try:
            p.wait(30)
        except subprocess.TimeoutExpired:
            p.kill()
    _llama["p"] = None


def _descarregar_dino():
    if _dino:
        _dino.clear()
        import torch

        torch.cuda.empty_cache()


def carregar_vlm():
    """Garante o llama-server de pé (inicia na primeira chamada ao VLM)."""
    if CARGA_SEQUENCIAL:
        _descarregar_dino()
    if LLAMA_URL or _llama_pronto():
        return
    gguf, mmproj = MODELOS_DIR / VLM_GGUF, MODELOS_DIR / VLM_MMPROJ
    for f in (gguf, mmproj):
        if not f.exists():
            raise RuntimeError(f"{f.name} não está em {MODELOS_DIR} (veja o README, seção Runpod)")
    _llama["p"] = subprocess.Popen([LLAMA_SERVER, "-m", str(gguf), "--mmproj", str(mmproj), "-c", "8192", "-ngl", "99",
                                    "--host", "127.0.0.1", "--port", str(LLAMA_PORTA), "--alias", VLM_ALIAS],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    limite = time.time() + 300
    while time.time() < limite:
        if _llama["p"].poll() is not None:
            raise RuntimeError(f"llama-server saiu com código {_llama['p'].returncode}")
        if _llama_pronto():
            return
        time.sleep(1)
    _parar_llama()
    raise RuntimeError("llama-server não ficou pronto em 300 s")


def carregar_dino(nome):
    import torch
    from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

    if CARGA_SEQUENCIAL and not LLAMA_URL:
        _parar_llama()
    if nome not in _dino:
        dispositivo = "cuda" if torch.cuda.is_available() else "cpu"
        _dino[nome] = (AutoProcessor.from_pretrained(nome),
                       AutoModelForZeroShotObjectDetection.from_pretrained(nome).to(dispositivo).eval())
    return _dino[nome]


def _imagem(b64):
    if len(b64) > MAX_IMAGEM:
        raise HTTPException(413, "imagem grande demais")
    try:
        img = Image.open(io.BytesIO(base64.b64decode(b64, validate=True)))
        img.load()
    except (binascii.Error, OSError, ValueError):
        raise HTTPException(422, "imagem inválida (JPEG ou PNG em base64)")
    return img.convert("RGB")


# ---------------- Rotas ----------------

@app.get("/saude")
def saude():
    """Sem autenticação e sem detalhes: só para o health check do Runpod."""
    return {"ok": True}


@app.get("/status", dependencies=[Depends(autenticar)])
def status():
    with _trava:
        lista = [t.resumo() for t in _trabalhos.values()]
    carregados = [f"Grounding DINO {n}" for n in _dino] + ([f"VLM {VLM_ALIAS}"] if _llama_pronto() else [])
    try:
        import importlib.util

        ocr = importlib.util.find_spec("paddleocr") is not None
        yolo = importlib.util.find_spec("ultralytics") is not None
    except Exception:
        ocr = yolo = False
    return {"servico": "geofat-gpu", "gpu": _gpu(), "modelos_carregados": carregados, "carga_sequencial": CARGA_SEQUENCIAL,
            "vlm": {"alias": VLM_ALIAS, "arquivos": all((MODELOS_DIR / f).exists() for f in (VLM_GGUF, VLM_MMPROJ)) or bool(LLAMA_URL),
                    "externo": bool(LLAMA_URL)},
            "ocr_disponivel": ocr, "yolo_instalado": yolo,
            "em_execucao": [t for t in lista if t["estado"] == "executando"],
            "na_fila": [t for t in lista if t["estado"] == "fila"],
            "recentes": [t for t in lista if t["estado"] not in ("fila", "executando")][-20:],
            "contadores": dict(_contadores)}


@app.get("/tarefas/{trabalho_id}", dependencies=[Depends(autenticar)])
def trabalho(trabalho_id: str):
    t = _trabalhos.get(trabalho_id)
    if not t:
        raise HTTPException(404, "trabalho não encontrado (o serviço guarda só os últimos 200)")
    return t.resumo()


@app.delete("/tarefas/{trabalho_id}", dependencies=[Depends(autenticar)])
def cancelar(trabalho_id: str):
    """Cancela um trabalho que ainda está na fila (o que já está na GPU termina)."""
    t = _trabalhos.get(trabalho_id)
    if not t or t.estado != "fila":
        raise HTTPException(409, "só dá para cancelar trabalho na fila")
    t.estado = "cancelado"
    t.pronto.set()
    return {"ok": True}


class Deteccao(BaseModel):
    imagem: str
    modelo: str = Field("IDEA-Research/grounding-dino-tiny", max_length=200)
    classes: dict[str, str] = Field(default_factory=lambda: {"fachada": "building facade."}, max_length=20)
    limiar: float = Field(0.35, ge=0.05, le=0.95)


@app.post("/detectar", dependencies=[Depends(autenticar)])
def detectar(d: Deteccao):
    """[[x1, y1, x2, y2], score, classe] em pixels da imagem enviada. Mesmo algoritmo de geofat/detectar.py."""
    img = _imagem(d.imagem)

    def rodar():
        import torch

        processador, rede = carregar_dino(d.modelo)
        frases = {t.strip().lower().rstrip(".").strip(): c for c, t in d.classes.items()}
        entrada = processador(images=img, text=". ".join(frases) + ".", return_tensors="pt").to(rede.device)
        with torch.no_grad():
            saida = rede(**entrada)
        r = processador.post_process_grounded_object_detection(
            saida, entrada.input_ids, threshold=d.limiar, text_threshold=0.25, target_sizes=[img.size[::-1]])[0]
        unica = next(iter(frases.values())) if len(frases) == 1 else None
        caixas = []
        for caixa, score, rotulo in zip(r["boxes"], r["scores"], r["text_labels"]):
            rotulo = rotulo.strip()
            classe = unica or frases.get(rotulo) or next((c for f, c in frases.items() if rotulo and (rotulo in f or f in rotulo)), None)
            if classe:
                caixas.append([[round(v, 1) for v in caixa.tolist()], float(score), classe])
        return caixas

    inicio = time.time()
    return {"caixas": submeter("detectar", rodar), "tempo_s": round(time.time() - inicio, 3)}


@app.get("/v1/models", dependencies=[Depends(autenticar)])
def modelos_v1():
    """Compatível com a OpenAI. Lista o VLM configurado sem carregá-lo (carrega na primeira pergunta)."""
    return {"object": "list", "data": [{"id": VLM_ALIAS, "object": "model"}]}


@app.post("/v1/chat/completions", dependencies=[Depends(autenticar)])
async def chat(request: Request):
    """Repassa ao llama-server (mesma API da OpenAI), pela fila, para não disputar a GPU com o detector."""
    corpo = await request.body()
    if len(corpo) > MAX_IMAGEM + 100_000:
        raise HTTPException(413, "requisição grande demais")
    try:
        pedido = json.loads(corpo)
    except ValueError:
        raise HTTPException(422, "JSON inválido")
    if pedido.get("model") != VLM_ALIAS:
        raise HTTPException(404, f"este serviço serve {VLM_ALIAS}, não {pedido.get('model')!r}")
    if pedido.get("stream"):
        raise HTTPException(422, "stream não suportado")

    def rodar():
        carregar_vlm()
        req = urllib.request.Request(_llama_url() + "/v1/chat/completions", data=corpo,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TEMPO_MAX_S) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"llama-server HTTP {e.code}: {e.read()[:300]!r}") from e

    import asyncio

    return await asyncio.to_thread(submeter, "vlm", rodar)


class Ocr(BaseModel):
    imagem: str
    idioma: str = Field("pt", max_length=10)


_ocr = {}


@app.post("/ocr", dependencies=[Depends(autenticar)])
def ocr(o: Ocr):
    img = _imagem(o.imagem)

    def rodar():
        import numpy as np

        try:
            from paddleocr import PaddleOCR
        except ImportError as e:
            raise RuntimeError("PaddleOCR não instalado nesta imagem (construa com --build-arg OCR=1)") from e
        if o.idioma not in _ocr:
            _ocr[o.idioma] = PaddleOCR(lang=o.idioma, use_doc_orientation_classify=False, use_doc_unwarping=False,
                                       use_textline_orientation=False)
        resultado = _ocr[o.idioma].predict(np.array(img))
        return "\n".join(t for r in resultado for t in r["rec_texts"]) or "NENHUM"

    return {"texto": submeter("ocr", rodar)}


@app.post("/modelos/descarregar", dependencies=[Depends(autenticar)])
def descarregar():
    """Libera a VRAM (detector e llama-server iniciado por este serviço)."""
    _descarregar_dino() if _dino else None
    _parar_llama()
    return {"ok": True, "gpu": _gpu()}
