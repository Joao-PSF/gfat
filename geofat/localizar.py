"""Associação geográfica observação → edificação, no PostGIS: setor de visão a partir da câmera, edificações
candidatas com evidências e a sugestão de onde está a fachada. Nunca usa o GSAN."""
import math

from psycopg.types.json import Jsonb

from .trajetorias import diferenca

ALCANCE_M = 60          # fachadas mais distantes que isso raramente são legíveis na foto
ERRO_BUSSOLA = 15       # graus: bússola de celular perto de carros e fiação
FOLGA_LADO_VIA = 30     # graus a mais quando a direção vem do lado da via (câmera presa ao veículo, não medida)
OB_CONFIAVEL = 0.8      # Open Buildings abaixo disso: polígono pouco confiável


def _raio_fisheye(theta, f, k1, k2):
    return f * theta * (1 + k1 * theta**2 + k2 * theta**4)


def angulo_da_coluna(cx, largura, fov, camera=None, altura=None):
    """Ângulo horizontal (graus, + à direita do eixo) da coluna x. Pinhole pelo fov, ou olho de peixe
    equidistante com distorção (modelo OpenSfM do Mapillary), em que o pinhole erra mais de 15° nas bordas."""
    if camera and camera.get("tipo") == "fisheye" and altura:
        f, k1, k2 = camera["parametros"]
        r = abs(cx - largura / 2) / max(largura, altura)
        theta = r / f
        for _ in range(20):  # Newton: raio(theta) = r
            erro = _raio_fisheye(theta, f, k1, k2) - r
            theta -= erro / (f * (1 + 3 * k1 * theta**2 + 5 * k2 * theta**4))
        return math.copysign(math.degrees(theta), cx - largura / 2)
    return math.degrees(math.atan((cx / largura - 0.5) * 2 * math.tan(math.radians(fov / 2))))


def coluna_do_angulo(delta, largura, fov, camera=None, altura=None):
    """Inverso de angulo_da_coluna: em que coluna da foto aparece um ponto `delta` graus fora do eixo."""
    if camera and camera.get("tipo") == "fisheye" and altura:
        f, k1, k2 = camera["parametros"]
        theta = math.radians(abs(delta))
        return largura / 2 + math.copysign(_raio_fisheye(theta, f, k1, k2) * max(largura, altura), delta)
    return largura * (0.5 + math.tan(math.radians(delta)) / (2 * math.tan(math.radians(fov / 2))))


def azimute(direcao, fov, cx, largura, camera=None, altura=None):
    """Azimute (graus) de uma coluna x da foto."""
    return (direcao + angulo_da_coluna(cx, largura, fov, camera, altura)) % 360


def eixo_da_camera(m):
    """(azimute do eixo, origem, folga extra) ou None. O lado da via só vale com rumo confiável."""
    if m["direcao"] is not None:
        return m["direcao"], "bussola", 0
    if m["rumo_confiavel"] and m["lado"]:
        return (m["rumo"] + (90 if m["lado"] == "direito" else -90)) % 360, "lado_via", FOLGA_LADO_VIA
    return None


CANDIDATAS = """
WITH s AS (
  SELECT cam, ST_MakeLine(cam, ST_Project(cam::geography, %(r)s, radians(%(az)s))::geometry) AS raio,
         ST_MakePolygon(ST_AddPoint(ST_MakeLine(array_prepend(cam, array_agg(
           ST_Project(cam::geography, %(r)s, radians(%(az)s - %(tol)s + i * %(tol)s / 8.0))::geometry ORDER BY i))), cam)) AS setor
  FROM (SELECT geom AS cam FROM foto WHERE id = %(foto)s) c, generate_series(0, 16) i GROUP BY cam
)
SELECT e.id, e.fonte, e.confianca,
       ST_Distance(e.geom::geography, s.cam::geography) AS distancia_m,
       ST_Intersects(e.geom, s.raio) AS no_raio,
       ST_Distance(ST_Intersection(e.geom, s.raio)::geography, s.cam::geography) AS distancia_raio_m,
       degrees(ST_Azimuth(s.cam, ST_Centroid(ST_Intersection(e.geom, s.setor)))) AS azimute_centro,
       ST_ClosestPoint(CASE WHEN ST_Intersects(e.geom, s.raio) THEN ST_Intersection(e.geom, s.raio)
                            ELSE ST_Intersection(e.geom, s.setor) END, s.cam) AS ponto
FROM edificacao e, s
WHERE ST_Intersects(e.geom, s.setor) AND NOT ST_Contains(e.geom, s.cam)  -- a câmera está na rua: conter = erro do GPS
"""


def candidatas(conn, m):
    """Edificações no setor de visão da observação, da mais para a menos provável, e as evidências gerais."""
    eixo = eixo_da_camera(m)
    ev = {"rumo": m["rumo"], "rumo_confiavel": m["rumo_confiavel"], "lado": m["lado"], "lado_fonte": m["lado_fonte"]}
    if eixo is None:
        ev["motivo"] = ("sem direção da câmera; lado da via não usado porque o sentido do deslocamento não é confiável"
                        if m["lado"] else "sem direção da câmera nem lado da via com sentido do deslocamento conhecido")
        return [], ev
    direcao, origem, folga = eixo
    x1, x2 = (m["bbox"][0], m["bbox"][2]) if m["bbox"] else (0, m["largura"])
    proj = (m["fov_horizontal"], m["largura"], m.get("camera"), m.get("altura"))
    az = azimute(direcao, proj[0], (x1 + x2) / 2, *proj[1:])
    meia = diferenca(azimute(direcao, proj[0], x2, *proj[1:]), azimute(direcao, proj[0], x1, *proj[1:])) / 2
    tol = min(80, max(meia, 5) + ERRO_BUSSOLA + folga)
    ev.update({"direcao": origem, "azimute": round(az, 1), "tolerancia": round(tol, 1)})
    linhas = conn.execute(CANDIDATAS, {"r": ALCANCE_M, "az": az, "tol": tol, "foto": m["foto_id"]}).fetchall()
    primeira = min((c["distancia_raio_m"] for c in linhas if c["no_raio"]), default=None)
    usa_lado = m["rumo_confiavel"] and m["lado"]
    saida, excluidas = [], 0
    for c in linhas:
        lado_c = None
        if m["rumo_confiavel"] and c["azimute_centro"] is not None:
            lado_c = "direito" if 0 < (c["azimute_centro"] - m["rumo"]) % 360 < 180 else "esquerdo"
        if usa_lado and lado_c != m["lado"]:
            excluidas += 1  # do outro lado da via
            continue
        desvio = 0 if c["no_raio"] else diferenca(c["azimute_centro"], az)
        e1 = c["no_raio"] and c["distancia_raio_m"] == primeira
        pont = 0.5 * (1 - min(desvio / tol, 1)) + 0.3 * (1 - min(c["distancia_m"] / ALCANCE_M, 1)) + 0.2 * e1
        evc = {"distancia_m": round(c["distancia_m"], 1), "desvio_graus": round(desvio, 1), "no_raio": c["no_raio"],
               "primeira_no_raio": e1, "lado": lado_c, "fonte": c["fonte"]}
        if c["fonte"] == "open_buildings" and (c["confianca"] or 0) < OB_CONFIAVEL:
            pont *= 0.8
            evc["poligono_pouco_confiavel"] = round(c["confianca"] or 0, 2)
        saida.append({"edificacao_id": c["id"], "pontuacao": round(pont, 3), "evidencias": evc, "ponto": c["ponto"]})
    ev["candidatas"] = len(saida)
    if excluidas:
        ev["excluidas_outro_lado"] = excluidas
    if not saida:
        ev["motivo"] = "nenhuma edificação no setor de visão" + (" do lado da via fotografado" if excluidas else "")
    return sorted(saida, key=lambda c: -c["pontuacao"]), ev


def localizar(conn, imovel_id=None):
    """Recalcula candidatas e a sugestão de localização (todas as observações, ou só `imovel_id`). Posições
    manuais não mudam. Sem edificação na linha de visada: fica na câmera, confiança baixa (pendente de revisão).
    Na execução geral, refaz antes as trajetórias e depois as sugestões de grupos (associação multivisual)."""
    if imovel_id is None:
        from .trajetorias import calcular

        calcular(conn)
    obs = conn.execute("""SELECT i.id, i.bbox, i.localizacao_fonte, f.id AS foto_id, f.direcao, f.fov_horizontal, f.largura, f.altura, f.camera,
                                 t.rumo, coalesce(t.rumo_confiavel, false) AS rumo_confiavel, t.lado, t.lado_fonte
                          FROM imovel i JOIN foto f ON f.id = i.foto_id LEFT JOIN foto_trajeto t ON t.foto_id = f.id
                          WHERE %(id)s::int IS NULL OR i.id = %(id)s""", {"id": imovel_id}).fetchall()
    contagem = {"direcao": 0, "lado_via": 0, "camera": 0}
    for m in obs:
        lista, ev = candidatas(conn, m)
        conn.execute("DELETE FROM candidato_edificacao WHERE imovel_id = %s", (m["id"],))
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO candidato_edificacao (imovel_id, edificacao_id, pontuacao, evidencias) VALUES (%s, %s, %s, %s)",
                            [(m["id"], c["edificacao_id"], c["pontuacao"], Jsonb(c["evidencias"])) for c in lista])
        if m["localizacao_fonte"] == "manual":
            conn.execute("UPDATE imovel SET localizacao_evidencias = %s WHERE id = %s", (Jsonb(ev), m["id"]))
            continue
        # Só sugere a edificação atingida pela linha de visada; candidatas só no setor ficam para o revisor
        escolhida = next((c for c in lista if c["evidencias"]["no_raio"]), None)
        if lista and not escolhida:
            ev["motivo"] = "nenhuma candidata na linha de visada; as do setor de visão ficam para revisão"
        if escolhida:
            fonte = "direcao" if ev["direcao"] == "bussola" else "lado_via"
            # Pelo lado da via a direção é estimada, não medida: fica sugerida com confiança baixa
            conn.execute("""UPDATE imovel SET geom = %s, edificacao_id = %s, localizacao_fonte = %s,
                            localizacao_confianca = %s, localizacao_evidencias = %s WHERE id = %s""",
                         (escolhida["ponto"], escolhida["edificacao_id"], fonte, "media" if fonte == "direcao" else "baixa",
                          Jsonb(ev), m["id"]))
        else:
            fonte = "camera"
            conn.execute("""UPDATE imovel SET geom = f.geom, edificacao_id = NULL, localizacao_fonte = 'camera',
                            localizacao_confianca = 'baixa', localizacao_evidencias = %s
                            FROM foto f WHERE f.id = imovel.foto_id AND imovel.id = %s""", (Jsonb(ev), m["id"]))
        contagem[fonte] += 1
    if imovel_id is None:
        from .multivisual import sugerir_grupos

        sugerir_grupos(conn)
    conn.commit()
    return contagem


def posicionar_manual(conn, imovel_id, edificacao_id=None, lat=None, lon=None):
    """Localização definida pelo revisor: numa edificação (ponto do contorno mais próximo da câmera)
    ou num ponto clicado no mapa (associa a edificação que o contém, se houver)."""
    if edificacao_id is not None:
        sql = """UPDATE imovel i SET edificacao_id = e.id,
                        geom = ST_ClosestPoint(ST_ExteriorRing(e.geom), f.geom),
                        localizacao_fonte = 'manual', localizacao_confianca = 'alta'
                 FROM edificacao e, foto f WHERE e.id = %s AND f.id = i.foto_id AND i.id = %s RETURNING i.id"""
        args = (edificacao_id, imovel_id)
    else:
        sql = """UPDATE imovel i SET geom = p.g, localizacao_fonte = 'manual', localizacao_confianca = 'alta',
                        edificacao_id = (SELECT id FROM edificacao WHERE ST_DWithin(geom::geography, p.g::geography, 2)
                                         ORDER BY geom <-> p.g LIMIT 1)
                 FROM (SELECT ST_SetSRID(ST_MakePoint(%s, %s), 4326) AS g) p WHERE i.id = %s RETURNING i.id"""
        args = (lon, lat, imovel_id)
    return conn.execute(sql, args).fetchone() is not None
