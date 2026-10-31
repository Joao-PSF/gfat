"""Trajetórias de captura: ordem das fotos, sentido do deslocamento (rumo) e lado da via fotografado.
O rumo sai do metadado (EXIF GPSTrack ou coordenadas.csv) ou das fotos vizinhas no percurso; só é marcado
como confiável quando o deslocamento supera o ruído do GPS e o percurso não faz curva entre as fotos."""
import math
from datetime import timedelta

from psycopg.types.json import Jsonb

INTERVALO_MAX = timedelta(seconds=120)  # mais que isso entre duas fotos = outro percurso
SALTO_MAX_M = 150
DESLOC_MIN_M = 8                         # abaixo disso o ruído típico do GPS (~5 m) domina o rumo
CURVA_MAX = 45


def distancia_m(a, b):
    la1, la2 = math.radians(a[1]), math.radians(b[1])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(math.radians(b[0] - a[0]) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


def rumo_entre(a, b):
    la1, la2, dl = math.radians(a[1]), math.radians(b[1]), math.radians(b[0] - a[0])
    x = math.sin(dl) * math.cos(la2)
    y = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dl)
    return math.degrees(math.atan2(x, y)) % 360


def diferenca(a, b):
    return abs((a - b + 180) % 360 - 180)


def lado(direcao, rumo):
    """Lado da via para onde a câmera aponta, dado o sentido do deslocamento. None = para frente ou para trás."""
    rel = (direcao - rumo) % 360
    return "direito" if 30 < rel < 150 else "esquerdo" if 210 < rel < 330 else None


def segmentar(fotos):
    """Percursos: o trajeto informado; sem ele, fotos da mesma pasta em sequência no tempo e no espaço."""
    trajetos = {}
    informadas = [f for f in fotos if f["trajeto"]]
    for f in sorted(informadas, key=lambda f: (f["trajeto"], f["sequencia"] is None, f["sequencia"] or 0,
                                               f["capturada_em"] is None, f["capturada_em"] or 0, f["id"])):
        trajetos.setdefault(f["trajeto"], []).append(f)
    resto = sorted((f for f in fotos if not f["trajeto"]),
                   key=lambda f: (f["arquivo"].rpartition("/")[0], f["capturada_em"] is None, f["capturada_em"] or 0, f["id"]))
    atual, anterior = None, None
    for f in resto:
        pasta = f["arquivo"].rpartition("/")[0]
        continua = (anterior is not None and f["capturada_em"] is not None and anterior["capturada_em"] is not None
                    and pasta == anterior["arquivo"].rpartition("/")[0]
                    and f["capturada_em"] - anterior["capturada_em"] <= INTERVALO_MAX
                    and distancia_m(f["xy"], anterior["xy"]) <= SALTO_MAX_M)
        if not continua:
            atual = f"{pasta or '.'}/" + (f["capturada_em"].isoformat(timespec="seconds") if f["capturada_em"] else f"foto{f['id']}")
            trajetos[atual] = []
        trajetos[atual].append(f)
        anterior = f
    return trajetos


def rumo_da_trajetoria(seq, i):
    """(rumo, confiável, evidências) da foto i pelas vizinhas; rumo None se não há deslocamento útil."""
    p = seq[i]
    limiar = max(DESLOC_MIN_M, 2 * (p["precisao_m"] or 0))
    antes, depois = seq[i - 1] if i else None, seq[i + 1] if i + 1 < len(seq) else None
    pernas = [(a, b) for a, b in ((antes, p), (p, depois)) if a and b and distancia_m(a["xy"], b["xy"]) >= limiar / 2]
    if not pernas:
        return None, False, {"motivo": "sem foto vizinha com deslocamento acima do ruído do GPS"}
    ini, fim = pernas[0][0], pernas[-1][1]
    desloc = distancia_m(ini["xy"], fim["xy"])
    ev = {"deslocamento_m": round(desloc, 1), "fotos": [ini["id"], fim["id"]]}
    rumo = rumo_entre(ini["xy"], fim["xy"]) if desloc > 0 else None
    confiavel = rumo is not None and desloc >= limiar
    if len(pernas) == 2:
        curva = diferenca(rumo_entre(*(x["xy"] for x in pernas[0])), rumo_entre(*(x["xy"] for x in pernas[1])))
        ev["curva_graus"] = round(curva)
        confiavel = confiavel and curva <= CURVA_MAX
    if not confiavel:
        ev["motivo"] = "curva entre as fotos" if ev.get("curva_graus", 0) > CURVA_MAX else "deslocamento pequeno"
    return rumo, confiavel, ev


def calcular(conn):
    """Refaz foto_trajeto para todas as fotos. Retorna {'trajetos': n, 'rumo_confiavel': n, 'lado': n}."""
    fotos = conn.execute("""SELECT id, arquivo, capturada_em, direcao, rumo, lado_via, trajeto, sequencia, precisao_m,
                                   ARRAY[ST_X(geom), ST_Y(geom)] AS xy FROM foto""").fetchall()
    linhas, conta = [], {"trajetos": 0, "rumo_confiavel": 0, "lado": 0}
    for nome, seq in segmentar(fotos).items():
        conta["trajetos"] += 1
        for i, f in enumerate(seq):
            if f["rumo"] is not None:
                rumo, fonte, confiavel, ev = f["rumo"], "informado", True, {}
            else:
                rumo, confiavel, ev = rumo_da_trajetoria(seq, i)
                fonte = "trajetoria" if rumo is not None else None
            calculado = lado(f["direcao"], rumo) if f["direcao"] is not None and confiavel else None
            if f["lado_via"]:
                lado_final, lado_fonte = f["lado_via"], "informado"
                if calculado and calculado != f["lado_via"]:
                    ev["conflito_lado"] = f"a direção da câmera indica o lado {calculado}"
            else:
                lado_final, lado_fonte = calculado, "direcao" if calculado else None
            conta["rumo_confiavel"] += confiavel
            conta["lado"] += lado_final is not None and confiavel
            linhas.append((f["id"], nome, i, rumo, fonte, confiavel, lado_final, lado_fonte, Jsonb(ev)))
    conn.execute("DELETE FROM foto_trajeto")
    with conn.cursor() as cur:
        cur.executemany("""INSERT INTO foto_trajeto (foto_id, trajeto, ordem, rumo, rumo_fonte, rumo_confiavel, lado,
                                                     lado_fonte, evidencias) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""", linhas)
    return conta
