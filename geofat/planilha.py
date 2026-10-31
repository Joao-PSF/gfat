"""Leitura de planilhas de imóveis (.xlsx ou .csv) para a análise direcionada. Só saem da leitura os campos que
localizam o imóvel; categoria, documento, débito e demais colunas são descartados aqui mesmo e nunca chegam à IA."""
import csv
import random
import re
import unicodedata
import zipfile
from xml.etree.ElementTree import iterparse

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
# coluna da planilha (sem acento, minúsculas) -> campo do alvo
NOMES = {"matricula": "matricula", "coordenada x": "lon", "longitude": "lon", "lon": "lon",
         "coordenada y": "lat", "latitude": "lat", "lat": "lat",
         "nome localidade": "localidade", "localidade": "localidade", "bairro": "localidade", "municipio": "municipio"}
OBRIGATORIOS = {"matricula", "lat", "lon"}
NO_BRASIL = lambda lat, lon: -34 <= lat <= 6 and -74 <= lon <= -28  # noqa: E731


def _norm(texto):
    return unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode().strip().lower()


def _linhas_xlsx(caminho):
    """Linhas da primeira aba como listas de textos, em fluxo (a planilha de exemplo tem 88 MB de XML)."""
    with zipfile.ZipFile(caminho) as z:
        compartilhadas = []
        if "xl/sharedStrings.xml" in z.namelist():
            with z.open("xl/sharedStrings.xml") as f:
                for _, el in iterparse(f):
                    if el.tag == NS + "si":
                        compartilhadas.append("".join(t.text or "" for t in el.iter(NS + "t")))
                        el.clear()
        aba = sorted(n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))[0]
        with z.open(aba) as f:
            for _, el in iterparse(f):
                if el.tag != NS + "row":
                    continue
                valores = {}
                for c in el.iter(NS + "c"):
                    coluna = re.match(r"[A-Z]+", c.get("r", "")).group()
                    k = 0
                    for letra in coluna:
                        k = k * 26 + ord(letra) - 64
                    v = c.find(NS + "v")
                    if c.get("t") == "s" and v is not None:
                        valores[k - 1] = compartilhadas[int(v.text)]
                    elif c.get("t") == "inlineStr":
                        valores[k - 1] = "".join(t.text or "" for t in c.iter(NS + "t"))
                    elif v is not None:
                        valores[k - 1] = v.text
                el.clear()
                yield [valores.get(k) for k in range(max(valores, default=-1) + 1)]


def _linhas_csv(caminho):
    with open(caminho, encoding="utf-8-sig", newline="") as f:
        dialeto = csv.Sniffer().sniff(f.readline(), ";,")
        f.seek(0)
        yield from csv.reader(f, dialect=dialeto)


def ler_alvos(caminho):
    """(válidos, inválidos): válidos são dicts linha, matricula, lat, lon, localidade, municipio, só com
    coordenada plausível no Brasil. X e Y trocados (latitude na coluna da longitude) são destrocados: no Brasil a
    longitude nunca cabe na faixa da latitude, então a troca não é ambígua. Repetições da mesma matrícula ficam com a primeira linha."""
    linhas = _linhas_xlsx(caminho) if str(caminho).lower().endswith(".xlsx") else _linhas_csv(caminho)
    cabecalho = next(linhas, None) or []
    indices = {NOMES[_norm(c)]: k for k, c in enumerate(cabecalho) if c and _norm(c) in NOMES}
    faltando = OBRIGATORIOS - set(indices)
    if faltando:
        raise ValueError(f"planilha sem as colunas: {', '.join(sorted(faltando))} (aceitas: Matricula, Coordenada X/Y "
                         "ou Longitude/Latitude)")
    validos, invalidos, vistos = [], 0, set()
    for n, r in enumerate(linhas, start=2):
        campo = lambda k: (r[indices[k]] if k in indices and indices[k] < len(r) else None)  # noqa: E731
        try:
            lat, lon = float(str(campo("lat")).replace(",", ".")), float(str(campo("lon")).replace(",", "."))
            matricula = re.sub(r"\.0$", "", str(campo("matricula") or "").strip())
            if not NO_BRASIL(lat, lon) and NO_BRASIL(lon, lat):
                lat, lon = lon, lat
            if not matricula or not NO_BRASIL(lat, lon):
                raise ValueError
        except (TypeError, ValueError):
            invalidos += 1
            continue
        if matricula in vistos:
            continue
        vistos.add(matricula)
        validos.append({"linha": n, "matricula": matricula, "lat": lat, "lon": lon,
                        "localidade": (campo("localidade") or "").strip() or None,
                        "municipio": (campo("municipio") or "").strip() or None})
    return validos, invalidos


def selecionar(validos, quantidade, semente=1, localidade=None, matriculas=None, cobertura=None, max_consultas=60):
    """Escolhe `quantidade` alvos: as matrículas pedidas, ou um sorteio reprodutível (semente) entre as linhas
    válidas, opcionalmente de uma localidade. cobertura(alvo) -> int (imagens perto) descarta alvos sem foto,
    com no máximo `max_consultas` consultas. Retorna (escolhidos, descartados_sem_foto)."""
    if matriculas:
        pedidas = [str(m).strip() for m in matriculas]
        por_matricula = {v["matricula"]: v for v in validos}
        candidatos = [por_matricula[m] for m in pedidas if m in por_matricula]
    else:
        candidatos = [v for v in validos if not localidade or _norm(v["localidade"] or "") == _norm(localidade)]
        candidatos = random.Random(semente).sample(candidatos, len(candidatos))
    escolhidos, sem_foto = [], []
    for c in candidatos:
        if len(escolhidos) >= quantidade or (cobertura and len(escolhidos) + len(sem_foto) >= max_consultas):
            break
        if cobertura:
            c = {**c, "imagens_perto": cobertura(c)}
            if not c["imagens_perto"]:
                sem_foto.append(c)
                continue
        escolhidos.append(c)
    return escolhidos, sem_foto
