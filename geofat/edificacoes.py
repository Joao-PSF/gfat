"""Polígonos de edificações: OpenStreetMap (Overpass) e Google Open Buildings v3."""
import csv
import gzip
import io
import json
import math
import urllib.parse
import urllib.request

INSERIR = """INSERT INTO edificacao (fonte, id_origem, confianca, geom)
             SELECT %s, %s, %s, g FROM ST_GeomFromText(%s, 4326) g WHERE ST_IsValid(g)
             ON CONFLICT (fonte, id_origem) DO UPDATE SET confianca = EXCLUDED.confianca, geom = EXCLUDED.geom"""


def area_das_fotos(conn, margem):
    e = conn.execute("""SELECT ST_YMin(x) s, ST_XMin(x) w, ST_YMax(x) n, ST_XMax(x) l
                        FROM (SELECT ST_Extent(geom) x FROM foto) a""").fetchone()
    if e["s"] is None:
        raise ValueError("Nenhuma foto importada: a área de busca vem das fotos.")
    return e["s"] - margem, e["w"] - margem, e["n"] + margem, e["l"] + margem


def importar_osm(conn, margem=0.002, area=None):
    """Contornos de edificações do OSM na área das fotos (ou em `area` = sul, oeste, norte, leste).
    Retorna quantidade importada."""
    s, w, n, l = area or area_das_fotos(conn, margem)
    consulta = f"[out:json][timeout:180];way[building]({s},{w},{n},{l});out geom;"
    req = urllib.request.Request("https://overpass-api.de/api/interpreter",
                                 data=urllib.parse.urlencode({"data": consulta}).encode(),
                                 headers={"User-Agent": "GeoFAT-CAEMA/0.2"})
    with urllib.request.urlopen(req, timeout=240) as r:
        elementos = json.load(r)["elements"]
    # shortcut: só ways fechados; multipolígonos (relations) ficam de fora, raros em casas e lojas
    linhas = []
    for el in elementos:
        pts = el.get("geometry", [])
        if len(pts) >= 4 and pts[0] == pts[-1]:
            wkt = "POLYGON((" + ",".join(f"{p['lon']} {p['lat']}" for p in pts) + "))"
            linhas.append(("osm", f"way/{el['id']}", None, wkt))
    with conn.cursor() as cur:
        cur.executemany(INSERIR, linhas)
    conn.commit()
    return len(linhas)


# --- Open Buildings: os arquivos são divididos por células S2 de nível 6 -------------------------
_IJ_PARA_POS = [[0, 1, 3, 2], [0, 3, 1, 2], [2, 3, 1, 0], [2, 1, 3, 0]]
_POS_PARA_ORIENTACAO = [1, 0, 0, 3]


def token_s2(lat, lon, nivel=6):
    """Token da célula S2 que contém o ponto (mesmo algoritmo da biblioteca S2 do Google)."""
    la, lo = math.radians(lat), math.radians(lon)
    x, y, z = math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)
    eixo = max(range(3), key=lambda k: abs((x, y, z)[k]))
    face = eixo if (x, y, z)[eixo] > 0 else eixo + 3
    u, v = [(y / x, z / x), (-x / y, z / y), (-x / z, -y / z),
            (z / x, y / x), (z / y, -x / y), (-y / z, -x / z)][face]
    st = lambda c: 0.5 * math.sqrt(1 + 3 * c) if c >= 0 else 1 - 0.5 * math.sqrt(1 - 3 * c)
    i, j = (min(2**30 - 1, max(0, int(st(c) * 2**30))) for c in (u, v))
    orientacao, pos = face & 1, 0
    for k in range(29, 29 - nivel, -1):
        p = _IJ_PARA_POS[orientacao][((i >> k) & 1) << 1 | ((j >> k) & 1)]
        pos = pos << 2 | p
        orientacao ^= _POS_PARA_ORIENTACAO[p]
    celula = face << 61 | pos << (61 - 2 * nivel) | 1 << (60 - 2 * nivel)
    return f"{celula:016x}".rstrip("0")


URL_OPEN_BUILDINGS = "https://storage.googleapis.com/open-buildings-data/v3/polygons_s2_level_6_gzip_no_header/{}_buildings.csv.gz"


def _arquivo_open_buildings(url):
    """O arquivo da célula S2 (~73 MB em São Luís) é baixado uma vez para data/cache e reaproveitado."""
    from .config import RAIZ

    destino = RAIZ / "data" / "cache" / "open_buildings" / url.rsplit("/", 1)[1]
    if not destino.exists():
        destino.parent.mkdir(parents=True, exist_ok=True)
        parcial = destino.with_suffix(".parcial")
        with urllib.request.urlopen(url, timeout=120) as r, open(parcial, "wb") as f:
            while bloco := r.read(1 << 20):
                f.write(bloco)
        parcial.replace(destino)
    return str(destino)


def importar_open_buildings(conn, arquivo=None, confianca_min=0.75, margem=0.002, area=None):
    """Lê o CSV(.gz) do Open Buildings (arquivo local ou, sem arquivo, o das células S2 da área das fotos ou de
    `area` = sul, oeste, norte, leste) e guarda só os polígonos dentro da área. Retorna quantidade importada."""
    s, w, n, l = area or area_das_fotos(conn, margem)
    if arquivo:
        fontes = [str(arquivo)]
    else:
        fontes = [_arquivo_open_buildings(u) for u in
                  sorted({URL_OPEN_BUILDINGS.format(token_s2(la, lo)) for la in (s, n) for lo in (w, l)})]
    csv.field_size_limit(10**8)
    linhas = []
    for fonte in fontes:
        print(f"lendo {fonte}")
        bruto = open(fonte, "rb")
        with bruto, (gzip.open(bruto, "rt", encoding="utf-8", newline="") if fonte.endswith(".gz")
                     else io.TextIOWrapper(bruto, encoding="utf-8", newline="")) as texto:
            # colunas: latitude, longitude, area_in_meters, confidence, geometry, full_plus_code
            for r in csv.reader(texto):
                try:
                    lat, lon, confianca = float(r[0]), float(r[1]), float(r[3])
                except ValueError:
                    continue  # cabeçalho
                if s <= lat <= n and w <= lon <= l and confianca >= confianca_min:
                    linhas.append(("open_buildings", r[5], confianca, r[4]))
    with conn.cursor() as cur:
        cur.executemany(INSERIR, linhas)
    conn.commit()
    return len(linhas)
