import csv
import math
from datetime import datetime

from PIL import Image, ImageOps

from .config import DECLINACAO_MAGNETICA, FOTOS_DIR

EXTENSOES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
GPS_IFD, EXIF_IFD, DATA_ORIGINAL, DATA, FOCAL_35MM = 0x8825, 0x8769, 36867, 306, 41989
FOCAL_PADRAO = 26  # mm equivalentes: câmera principal típica de celular


def abrir(caminho):
    """Imagem RGB já girada conforme o EXIF, igual ao que o navegador mostra."""
    with Image.open(caminho) as img:
        return ImageOps.exif_transpose(img).convert("RGB")


def _graus(dms, ref):
    graus = float(dms[0]) + float(dms[1]) / 60 + float(dms[2]) / 3600
    return -graus if ref in ("S", "W") else graus


def fov_horizontal(focal_35mm, largura, altura):
    """Campo de visão horizontal (graus) pela focal equivalente a 35 mm (diagonal de 43,27 mm)."""
    largura_mm = 43.27 * largura / math.hypot(largura, altura)
    return math.degrees(2 * math.atan(largura_mm / (2 * (focal_35mm or FOCAL_PADRAO))))


def ler_metadados(caminho):
    """Retorna dict com lat, lon, direcao, rumo e precisao_m (None se ausentes), capturada_em, largura, altura e fov."""
    with Image.open(caminho) as img:
        exif = img.getexif()
        largura, altura = img.size
        if exif.get(0x0112) in (5, 6, 7, 8):  # orientação EXIF que gira 90°
            largura, altura = altura, largura
    gps, dados_exif = exif.get_ifd(GPS_IFD), exif.get_ifd(EXIF_IFD)
    lat = lon = direcao = rumo = None
    if 2 in gps and 4 in gps:
        lat, lon = _graus(gps[2], gps.get(1)), _graus(gps[4], gps.get(3))
    if 17 in gps:
        # 'M' = norte magnético; corrige pela declinação local para chegar ao norte verdadeiro
        direcao = (float(gps[17]) + (DECLINACAO_MAGNETICA if gps.get(16) == "M" else 0)) % 360
    if 15 in gps:  # GPSTrack: sentido do deslocamento
        rumo = (float(gps[15]) + (DECLINACAO_MAGNETICA if gps.get(14) == "M" else 0)) % 360
    texto = dados_exif.get(DATA_ORIGINAL) or exif.get(DATA)
    try:
        capturada_em = datetime.strptime(texto.strip("\x00 "), "%Y:%m:%d %H:%M:%S") if texto else None
    except ValueError:
        capturada_em = None
    return {"lat": lat, "lon": lon, "direcao": direcao, "rumo": rumo, "precisao_m": float(gps[31]) if 31 in gps else None,
            "capturada_em": capturada_em, "largura": largura,
            "altura": altura, "fov": fov_horizontal(dados_exif.get(FOCAL_35MM), largura, altura)}


def ler_coordenadas(pasta):
    """coordenadas.csv opcional na pasta (arquivo;latitude;longitude;direcao;credito e, opcionais, rumo;lado;
    trajeto;sequencia), para fotos cujos metadados vêm separados da imagem. Tem prioridade sobre o EXIF.
    lado = lado da via que a câmera fotografa (direito/esquerdo); rumo = sentido do deslocamento, em graus."""
    caminho = pasta / "coordenadas.csv"
    if not caminho.exists():
        return {}
    with open(caminho, encoding="utf-8-sig", newline="") as f:
        linhas = list(csv.DictReader(f, delimiter=";"))
    num = lambda v: float(v.replace(",", ".")) if v and v.strip() else None
    saida = {}
    for n, r in enumerate(linhas, 2):
        lado = (r.get("lado") or "").strip().lower() or None
        if lado not in (None, "direito", "esquerdo"):
            raise ValueError(f"{caminho}, linha {n}: lado deve ser 'direito' ou 'esquerdo'")
        sequencia = (r.get("sequencia") or "").strip()
        saida[r["arquivo"]] = {"lat": num(r["latitude"]), "lon": num(r["longitude"]), "direcao": num(r.get("direcao")),
                               "credito": r.get("credito") or None, "rumo": num(r.get("rumo")), "lado": lado,
                               "trajeto": (r.get("trajeto") or "").strip() or None,
                               "sequencia": int(sequencia) if sequencia else None}
    return saida


def importar_fotos(conn, fonte, pasta=None, raiz=FOTOS_DIR):
    """Registra as fotos novas de `pasta` (padrão: toda a raiz). Retorna (novas, sem_gps, erros)."""
    novas, sem_gps, erros, coordenadas = 0, [], [], {}
    for caminho in sorted((pasta or raiz).rglob("*")):
        if caminho.suffix.lower() not in EXTENSOES:
            continue
        if caminho.parent not in coordenadas:
            coordenadas[caminho.parent] = ler_coordenadas(caminho.parent)
        try:
            m = ler_metadados(caminho)
            externo = coordenadas[caminho.parent].get(caminho.name, {})
            m.update({k: v for k, v in externo.items() if v is not None})
        except Exception as e:
            erros.append(f"{caminho}: {e}")
            continue
        if m["lat"] is None:
            sem_gps.append(str(caminho))
            continue
        cur = conn.execute(
            """INSERT INTO foto (arquivo, fonte, capturada_em, largura, altura, direcao, fov_horizontal, credito, rumo,
                                 lado_via, trajeto, sequencia, precisao_m, geom)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
               ON CONFLICT (arquivo) DO NOTHING""",
            (caminho.relative_to(raiz).as_posix(), fonte, m["capturada_em"], m["largura"], m["altura"],
             m["direcao"], m["fov"], m.get("credito"), m["rumo"], m.get("lado"), m.get("trajeto"), m.get("sequencia"),
             m["precisao_m"], m["lon"], m["lat"]),
        )
        novas += cur.rowcount
    conn.commit()
    return novas, sem_gps, erros
