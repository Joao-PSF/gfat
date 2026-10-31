"""Classificação do uso do imóvel pela fachada, com o Bonsai rodando localmente."""
import base64
import io
import json
import re
import time
import unicodedata
import urllib.error
import urllib.request

from .config import FOTOS_DIR
from .fotos import abrir
from .tarefas import ContextoConsole

CATEGORIAS = ("residencial", "comercial", "industrial", "institucional", "misto", "terreno_vazio", "inconclusivo")

PROMPT = """Você analisa fotos de fachadas para uma companhia de saneamento. Classifique o uso do imóvel em destaque na imagem:
- residencial: moradia (casa, prédio de apartamentos)
- comercial: loja, escritório, serviço, oficina, restaurante, hotel
- industrial: fábrica, galpão de produção
- institucional: órgão público, escola, hospital, posto de saúde, igreja, museu
- misto: moradia e atividade não residencial no mesmo imóvel (ex.: loja embaixo, casa em cima)
- terreno_vazio: lote sem construção (terreno baldio, só mato, muro ou cerca sem edificação atrás)
- inconclusivo: a imagem não permite decidir (fachada encoberta, distante, borrada, sem indícios de uso)
Use só o que é visível: letreiros, portas de aço, vitrines, placas, brasões, janelas com cortinas, varais, grades.
Estime quantas unidades independentes existem (casas, apartamentos, lojas, salas), contando portas de acesso, números, campainhas, medidores e andares.
Responda somente com JSON:
{"categoria": "residencial|comercial|industrial|institucional|misto|terreno_vazio|inconclusivo", "unidades": 1, "confianca": 0.0, "evidencias": "o que você viu que justifica", "limitacoes": "o que impediu mais certeza"}"""

PROMPT_OCR = """Transcreva todo o texto legível nesta fachada (letreiros, placas, números, faixas), uma linha por texto.
Não interprete nem traduza. Se não houver texto legível, responda apenas: NENHUM"""

# Modo sem raciocínio com a amostragem recomendada pela PrismML para o Bonsai: no CPU o raciocínio gasta milhares de tokens
PARAMETROS_BONSAI = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 1.5, "max_tokens": 1024,
                     "chat_template_kwargs": {"enable_thinking": False}}


def interpretar(texto):
    """Extrai (categoria, unidades, confianca, evidencias, limitacoes) da resposta do modelo; ValueError se inválida."""
    texto = re.sub(r"<think>.*?</think>", "", texto, flags=re.S)
    achado = re.search(r"\{.*\}", texto, re.S)
    if not achado:
        raise ValueError(f"resposta sem JSON: {texto[:200]!r}")
    dados = json.loads(achado.group())
    categoria = unicodedata.normalize("NFKD", str(dados.get("categoria", ""))).encode("ascii", "ignore")
    # o modelo às vezes devolve a linha do prompt inteira ("residencial: moradia (...)"): vale a 1ª palavra
    categoria = (re.match(r"\s*(terreno[ _]vazio|[a-z]*)", categoria.decode().lower()) or [""])[1].replace(" ", "_")
    if categoria not in CATEGORIAS:
        raise ValueError(f"categoria desconhecida: {dados.get('categoria')!r}")
    unidades = dados.get("unidades")
    unidades = max(1, int(unidades)) if isinstance(unidades, (int, float)) or str(unidades).isdigit() else None
    confianca = min(1.0, max(0.0, float(dados.get("confianca") or 0)))
    return (categoria, unidades, confianca, str(dados.get("evidencias") or "")[:2000],
            str(dados.get("limitacoes") or "")[:2000])


def recortar(img, bbox, margem=0.1):
    """JPEG da fachada com margem de contexto, no máximo 1024 px."""
    if bbox:
        x1, y1, x2, y2 = bbox
        mx, my = (x2 - x1) * margem, (y2 - y1) * margem
        img = img.crop((max(0, x1 - mx), max(0, y1 - my), min(img.width, x2 + mx), min(img.height, y2 + my)))
    img.thumbnail((1024, 1024))
    saida = io.BytesIO()
    img.save(saida, "JPEG", quality=85)
    return saida.getvalue()


def perguntar_vlm(jpeg, url, modelo, prompt=PROMPT, parametros=None, chave=None, timeout=900):
    """Pergunta a um modelo de visão por uma API compatível com a da OpenAI (llama-server, LM Studio, Ollama,
    OpenRouter). O recorte é JPEG recodificado: sai sem EXIF, ou seja, sem a localização GPS da foto."""
    corpo = {"model": modelo, **(parametros or {}), "messages": [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}},
    ]}]}
    cabecalhos = {"Content-Type": "application/json"}
    if chave:
        cabecalhos["Authorization"] = f"Bearer {chave}"
    req = urllib.request.Request(url.rstrip("/") + "/chat/completions", data=json.dumps(corpo).encode(), headers=cabecalhos)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # CPU sem GPU pode levar minutos por imagem
        return json.load(r)["choices"][0]["message"]["content"]


def classificar(conn, limite=None, perguntar=None, nome_modelo=None, ctx=None, ler_texto=None):
    """Classifica imóveis ainda sem categoria da IA. Falhas ficam pendentes para nova tentativa.
    perguntar(jpeg, prompt) -> texto do modelo; ler_texto(jpeg) -> texto da fachada (OCR opcional)."""
    ctx = ctx or ContextoConsole()
    pendentes = conn.execute(
        """SELECT i.id, i.bbox, f.arquivo FROM imovel i JOIN foto f ON f.id = i.foto_id
           WHERE i.categoria_ia IS NULL AND i.fachada_valida IS DISTINCT FROM false
           ORDER BY i.id LIMIT %s""", (limite,)).fetchall()
    ok, falhas, inicio = 0, [], time.time()
    ctx.progresso(0, len(pendentes), unidade="fachadas")
    for n, p in enumerate(pendentes):
        ctx.verificar()
        ctx.progresso(n, len(pendentes))
        texto = None
        try:
            jpeg = recortar(abrir(FOTOS_DIR / p["arquivo"]), p["bbox"])
            prompt = PROMPT
            if ler_texto:
                texto = ler_texto(jpeg).strip()
                if texto and texto.upper() != "NENHUM":
                    prompt += f"\nTexto lido na fachada por OCR (pode ter erros): {texto[:500]}"
            categoria, unidades, confianca, evidencias, limitacoes = interpretar(perguntar(jpeg, prompt))
        except urllib.error.URLError as e:
            if isinstance(e.reason, ConnectionRefusedError):
                falhas.append(f"servidor do modelo fora do ar; {len(pendentes) - ok - len(falhas)} imóvel(is) pendente(s)")
                break
            falhas.append(f"imóvel {p['id']}: {e}")
            continue
        except Exception as e:
            falhas.append(f"imóvel {p['id']}: {e}")
            continue
        # Só grava se a caixa não foi redesenhada enquanto o modelo trabalhava
        gravou = conn.execute(
            """UPDATE imovel SET categoria_ia = %s, unidades_ia = %s, confianca_ia = %s, evidencias_ia = %s,
                      limitacoes_ia = %s, modelo_ia = %s, texto_ocr = %s, classificado_em = now()
               WHERE id = %s AND bbox IS NOT DISTINCT FROM %s::int[]""",
            (categoria, unidades, confianca, evidencias, limitacoes, nome_modelo, texto, p["id"], p["bbox"])).rowcount
        conn.commit()
        if not gravou:
            falhas.append(f"imóvel {p['id']}: caixa alterada durante a classificação, fica pendente")
            continue
        ok += 1
        ctx.log(f"imóvel {p['id']}: {categoria}, {unidades} unidade(s), confiança {confianca:.2f} "
                f"({time.time() - inicio:.0f} s)")
        inicio = time.time()
    ctx.progresso(len(pendentes), len(pendentes))
    return ok, falhas
