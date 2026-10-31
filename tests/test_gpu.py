"""Serviço GPU (gpu/servico.py) sem GPU e sem modelos: autenticação, rotas, fila e repasse ao llama-server."""
import importlib.util
import io
import json

import pytest
from fastapi.testclient import TestClient

from geofat.config import RAIZ

TOKEN = "t" * 32


@pytest.fixture
def gpu(monkeypatch):
    monkeypatch.setenv("GEOFAT_GPU_TOKEN", TOKEN)
    monkeypatch.setenv("LLAMA_URL", "http://llama.falso")
    spec = importlib.util.spec_from_file_location("servico_gpu", RAIZ / "gpu" / "servico.py")
    s = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(s)
    return s, TestClient(s.app)


def test_servico_gpu_exige_token(gpu, monkeypatch):
    _, c = gpu
    assert c.get("/saude").json() == {"ok": True}  # health check do Runpod, sem dados
    for rota in ("/status", "/v1/models"):
        assert c.get(rota).status_code == 401
        assert c.get(rota, headers={"Authorization": "Bearer errado"}).status_code == 401
    assert c.post("/detectar", json={"imagem": "x"}).status_code == 401
    monkeypatch.setenv("GEOFAT_GPU_TOKEN", "curto")
    spec = importlib.util.spec_from_file_location("servico_gpu2", RAIZ / "gpu" / "servico.py")
    with pytest.raises(SystemExit):
        spec.loader.exec_module(importlib.util.module_from_spec(spec))


def test_servico_gpu_repassa_ao_llama_pela_fila(gpu, monkeypatch):
    s, c = gpu
    cab = {"Authorization": f"Bearer {TOKEN}"}
    assert c.get("/v1/models", headers=cab).json()["data"][0]["id"] == s.VLM_ALIAS
    assert c.post("/v1/chat/completions", headers=cab, json={"model": "outro"}).status_code == 404
    pedidos = []

    class Resposta(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def urlopen(req, timeout=None):
        pedidos.append(req.full_url)
        return Resposta(json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode())
    monkeypatch.setattr(s.urllib.request, "urlopen", urlopen)
    r = c.post("/v1/chat/completions", headers=cab, json={"model": s.VLM_ALIAS, "messages": []})
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "{}"
    assert pedidos[-1] == "http://llama.falso/v1/chat/completions"
    assert c.post("/detectar", headers=cab, json={"imagem": "não é base64"}).status_code == 422
    st = c.get("/status", headers=cab).json()
    assert st["contadores"] == {"concluido": 1} and st["recentes"][-1]["tipo"] == "vlm" and st["vlm"]["externo"]
