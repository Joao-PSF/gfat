"""Inicia e para o llama-server local que serve o Bonsai. Só aceita modelos GGUF da pasta modelos/ e
argumentos fixos (os mesmos do README): a interface web não executa comandos arbitrários."""
import subprocess
import sys
import urllib.parse
import urllib.request

from .config import RAIZ

FERRAMENTAS = RAIZ / "ferramentas"
LOG = RAIZ / "data" / "logs" / "llama-server.log"
_processo = {"p": None, "modelo": None}


def executavel():
    for pasta in ("llama-vulkan", "llama-cpu"):
        for nome in ("llama-server.exe", "llama-server"):
            if (FERRAMENTAS / pasta / nome).exists():
                return FERRAMENTAS / pasta / nome
    return None


def modelos_gguf():
    """Pares modelo + projetor de visão (mmproj) em modelos/. Sem mmproj o modelo não vê imagens."""
    pasta = RAIZ / "modelos"
    pares = {}
    for g in sorted(pasta.glob("*.gguf")):
        if "mmproj" not in g.name:
            prefixo = g.name.split("-Q")[0].split("-PQ")[0].split("-PTQ")[0]
            mmproj = sorted(pasta.glob(f"{prefixo}-mmproj-*.gguf"))
            if mmproj:
                pares[g.stem] = (g, mmproj[0])
    return pares


def porta(url):
    u = urllib.parse.urlparse(url)
    if u.hostname not in ("localhost", "127.0.0.1"):
        return None
    return u.port or 80


def estado(url):
    p = _processo["p"]
    rodando = p is not None and p.poll() is None
    try:
        # 127.0.0.1 e timeout curto: no Windows, conexão recusada em "localhost" demora ~2 s (tenta IPv6 e repete)
        saude = url.rstrip("/").removesuffix("/v1").replace("//localhost", "//127.0.0.1") + "/health"
        with urllib.request.urlopen(saude, timeout=0.5) as r:
            pronto = r.status == 200
    except Exception:
        pronto = False
    return {"executavel": executavel() is not None, "gerenciado": rodando, "respondendo": pronto,
            "modelo": _processo["modelo"] if rodando else None, "modelos": list(modelos_gguf()),
            "saida": p.returncode if p is not None and not rodando else None}


def iniciar(url, modelo):
    pares, exe, p = modelos_gguf(), executavel(), porta(url)
    if modelo not in pares:
        raise ValueError("modelo GGUF não encontrado em modelos/")
    if not exe:
        raise ValueError("llama-server não encontrado em ferramentas/ (veja o README)")
    if p is None:
        raise ValueError("a conexão do Bonsai não aponta para localhost")
    parar()
    gguf, mmproj = pares[modelo]
    LOG.parent.mkdir(parents=True, exist_ok=True)
    # --no-mmproj-offload e lotes de 256: evitam o reset da GPU integrada pelo Windows (TDR), ver README
    args = [str(exe), "-m", str(gguf), "--mmproj", str(mmproj), "--no-mmproj-offload", "-c", "8192", "-ngl", "99",
            "-b", "256", "-ub", "256", "--host", "127.0.0.1", "--port", str(p), "--alias", modelo]
    with open(LOG, "ab") as log:
        _processo["p"] = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT,
                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0)
    _processo["modelo"] = modelo


def parar():
    p = _processo["p"]
    if p is not None and p.poll() is None:
        p.terminate()
        try:
            p.wait(15)
        except subprocess.TimeoutExpired:
            p.kill()
    _processo["p"] = None
