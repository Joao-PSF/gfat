"""Dados públicos complementares: CNEFE (IBGE) e OpenStreetMap."""
import csv
import json
import urllib.parse
import urllib.request

from .enderecos import normalizar

INSERIR = """INSERT INTO referencia_publica (fonte, id_origem, categoria, descricao, logradouro, numero, geom)
             VALUES (%s, %s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
             ON CONFLICT (fonte, id_origem) DO UPDATE
             SET categoria = EXCLUDED.categoria, descricao = EXCLUDED.descricao, logradouro = EXCLUDED.logradouro,
                 numero = EXCLUDED.numero, geom = EXCLUDED.geom"""

# COD_ESPECIE do CNEFE 2022.
ESPECIE_CNEFE = {
    "1": ("residencial", "domicílio particular"),
    "2": ("residencial", "domicílio coletivo"),
    "3": (None, "estabelecimento agropecuário"),
    "4": ("institucional", "estabelecimento de ensino"),
    "5": ("institucional", "estabelecimento de saúde"),
    "6": ("comercial", "estabelecimento de outras finalidades"),
    "7": (None, "edificação em construção"),
    "8": ("institucional", "estabelecimento religioso"),
}


def importar_cnefe(conn, caminho, encoding="utf-8-sig"):
    """Importa o CSV do CNEFE de um município (separador ';'). Retorna (importados, ignorados)."""
    linhas, ignorados = [], 0
    with open(caminho, encoding=encoding, newline="") as f:
        leitor = csv.DictReader(f, delimiter=";")
        faltando = {"COD_UNICO_ENDERECO", "COD_ESPECIE", "LATITUDE", "LONGITUDE"} - set(leitor.fieldnames or [])
        if faltando:
            raise ValueError(f"CSV do CNEFE sem as colunas: {', '.join(sorted(faltando))}")
        for r in leitor:
            try:
                lat, lon = float(r["LATITUDE"].replace(",", ".")), float(r["LONGITUDE"].replace(",", "."))
            except (ValueError, AttributeError):
                ignorados += 1
                continue
            categoria, especie = ESPECIE_CNEFE.get(r["COD_ESPECIE"], (None, f"espécie {r['COD_ESPECIE']}"))
            logradouro = " ".join(filter(None, (r.get("NOM_TIPO_SEGLOGR"), r.get("NOM_TITULO_SEGLOGR"), r.get("NOM_SEGLOGR"))))
            endereco = " ".join(filter(None, (logradouro, r.get("NUM_ENDERECO"))))
            descricao = " · ".join(filter(None, (especie, r.get("DSC_ESTABELECIMENTO"), endereco)))
            linhas.append(("cnefe", r["COD_UNICO_ENDERECO"], categoria, descricao, logradouro or None,
                           normalizar(r.get("NUM_ENDERECO")), lon, lat))
    with conn.cursor() as cur:
        cur.executemany(INSERIR, linhas)
    conn.commit()
    return len(linhas), ignorados


AMENITY_INSTITUCIONAL = {"townhall", "police", "fire_station", "courthouse", "library", "social_facility",
                         "community_centre", "post_office", "prison", "school", "university", "college",
                         "kindergarten", "hospital", "clinic", "doctors", "place_of_worship", "arts_centre"}
AMENITY_COMERCIAL = {"restaurant", "bar", "cafe", "fast_food", "pub", "pharmacy", "bank", "fuel",
                     "car_wash", "marketplace", "ice_cream", "dentist", "veterinary", "bureau_de_change"}
BUILDING = {"house": "residencial", "residential": "residencial", "apartments": "residencial",
            "detached": "residencial", "commercial": "comercial", "retail": "comercial",
            "industrial": "industrial", "public": "institucional", "civic": "institucional", "government": "institucional",
            "school": "institucional", "hospital": "institucional", "church": "institucional"}


def categoria_osm(tags):
    if (tags.get("office") == "government" or tags.get("amenity") in AMENITY_INSTITUCIONAL
            or tags.get("tourism") == "museum"):
        return "institucional"
    if "shop" in tags or "office" in tags or "craft" in tags or tags.get("amenity") in AMENITY_COMERCIAL:
        return "comercial"
    if tags.get("landuse") == "industrial" or tags.get("man_made") == "works":
        return "industrial"
    return BUILDING.get(tags.get("building"))


def importar_osm(conn, margem=0.003):
    """Baixa do Overpass os pontos de uso na área das fotos (com margem em graus). Retorna importados."""
    e = conn.execute("""SELECT ST_YMin(x) s, ST_XMin(x) w, ST_YMax(x) n, ST_XMax(x) l
                        FROM (SELECT ST_Extent(geom) x FROM foto) a""").fetchone()
    if e["s"] is None:
        raise ValueError("Nenhuma foto importada: a área de busca no OSM vem das fotos.")
    b = f"{e['s'] - margem},{e['w'] - margem},{e['n'] + margem},{e['l'] + margem}"
    tipos = "|".join(BUILDING)
    consulta = (f'[out:json][timeout:120];(nwr[shop]({b});nwr[office]({b});nwr[craft]({b});'
                f'nwr[amenity]({b});nwr[landuse=industrial]({b});nwr[tourism=museum]({b});nwr[building~"^({tipos})$"]({b}););'
                'out center tags;')
    req = urllib.request.Request("https://overpass-api.de/api/interpreter",
                                 data=urllib.parse.urlencode({"data": consulta}).encode(),
                                 headers={"User-Agent": "GeoFAT-CAEMA/0.1"})
    with urllib.request.urlopen(req, timeout=180) as r:
        elementos = json.load(r)["elements"]
    linhas = []
    for el in elementos:
        tags = el.get("tags", {})
        ponto = el if "lat" in el else el.get("center")
        if not ponto:
            continue
        chaves = ("shop", "office", "craft", "amenity", "building", "landuse")
        descricao = " · ".join(filter(None, [tags.get("name")] + [f"{k}={tags[k]}" for k in chaves if k in tags]))
        linhas.append(("osm", f"{el['type']}/{el['id']}", categoria_osm(tags), descricao, tags.get("addr:street"),
                       normalizar(tags.get("addr:housenumber")), ponto["lon"], ponto["lat"]))
    with conn.cursor() as cur:
        cur.executemany(INSERIR, linhas)
    conn.commit()
    return len(linhas)
