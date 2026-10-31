"""Associação geográfica, trajetórias, várias observações do mesmo imóvel, unidades e endereços, com coordenadas
sintéticas mínimas (sem imagens). Não valida reconhecimento visual nem OCR: só a geometria e o armazenamento."""
from datetime import datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from geofat import multivisual
from geofat.config import DATABASE_URL
from geofat.enderecos import ler_enderecos
from geofat.localizar import localizar
from geofat.trajetorias import calcular
from test_banco import LAT, conn, edificacao, imovel  # noqa: F401  (fixture e ajudantes)

LON = -44.3000
M = 1 / 111320  # graus por metro, perto do equador
T0 = datetime(2026, 1, 10, 9, 0, 0)


def captura(conn, arquivo, norte_m=0.0, leste_m=0.0, segundos=0, direcao=None, lado=None):
    return conn.execute("""INSERT INTO foto (arquivo, fonte, capturada_em, largura, altura, direcao, fov_horizontal, lado_via, geom)
                           VALUES (%s, 'captura_propria', %s, 1000, 750, %s, 60, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
                           RETURNING id""",
                        (arquivo, T0 + timedelta(seconds=segundos), direcao, lado, LON + leste_m * M, LAT + norte_m * M)).fetchone()["id"]


def caixa_m(conn, oeste, sul, leste, norte, nome):
    """Edificação retangular em metros a partir de (LON, LAT)."""
    return edificacao(conn, LON + oeste * M, LAT + sul * M, LON + leste * M, LAT + norte * M, nome)


def trajeto(conn, foto_id):
    return conn.execute("SELECT * FROM foto_trajeto WHERE foto_id = %s", (foto_id,)).fetchone()


def obs(conn, i):
    return conn.execute("SELECT * FROM imovel WHERE id = %s", (i,)).fetchone()


def test_trajetoria_rumo_e_lado(conn):
    # Três fotos a cada 11 m rumo ao norte, câmera olhando para leste: rumo 0°, lado direito
    a, b, c = (captura(conn, f"rua/{k}.jpg", 11 * k, segundos=5 * k, direcao=90) for k in range(3))
    sozinha = captura(conn, "rua/depois.jpg", 40, segundos=600, direcao=90)        # 10 min depois: outro percurso
    curva = [captura(conn, "curva/1.jpg", 0, 100), captura(conn, "curva/2.jpg", 11, 100, 5),
             captura(conn, "curva/3.jpg", 11, 111, 10)]                            # vira 90° na foto do meio
    parada = [captura(conn, "parada/1.jpg", 0, 200), captura(conn, "parada/2.jpg", 2, 200, 5)]  # 2 m: ruído do GPS
    calcular(conn)

    t = trajeto(conn, b)
    assert round(t["rumo"]) % 360 == 0 and t["rumo_confiavel"] and t["rumo_fonte"] == "trajetoria"
    assert (t["lado"], t["lado_fonte"]) == ("direito", "direcao")
    assert trajeto(conn, a)["rumo_confiavel"] and trajeto(conn, c)["trajeto"] == t["trajeto"]
    s = trajeto(conn, sozinha)
    assert s["trajeto"] != t["trajeto"] and s["rumo"] is None and not s["rumo_confiavel"] and s["lado"] is None
    m = trajeto(conn, curva[1])
    assert not m["rumo_confiavel"] and m["evidencias"]["curva_graus"] == 90
    assert not trajeto(conn, parada[0])["rumo_confiavel"]


def test_lado_da_via_so_com_rumo_confiavel(conn):
    # Câmera presa do lado direito, sem bússola, andando para o norte: a fachada fica a leste
    leste = caixa_m(conn, 8, 5, 20, 17, "leste")
    caixa_m(conn, -20, 5, -8, 17, "oeste")
    fotos = [captura(conn, f"carro/{k}.jpg", 11 * k, segundos=4 * k, lado="direito") for k in range(3)]
    meio = imovel(conn, fotos[1])
    isolada = imovel(conn, captura(conn, "avulsa/1.jpg", 11, lado="direito"))  # sem vizinhas: rumo desconhecido
    assert localizar(conn) == {"direcao": 0, "lado_via": 1, "camera": 1}

    o = obs(conn, meio)
    assert (o["edificacao_id"], o["localizacao_fonte"], o["localizacao_confianca"]) == (leste, "lado_via", "baixa")
    assert o["localizacao_evidencias"]["direcao"] == "lado_via" and round(o["localizacao_evidencias"]["azimute"]) == 90
    o = obs(conn, isolada)
    assert o["localizacao_fonte"] == "camera" and o["edificacao_id"] is None
    assert "não é confiável" in o["localizacao_evidencias"]["motivo"]


def test_lado_da_via_exclui_edificacao_do_outro_lado(conn):
    # Bússola apontando 10° (quase para frente); uma casa à frente-direita e outra à frente-esquerda no setor
    ne = caixa_m(conn, 3, 29, 10, 35, "ne")
    caixa_m(conn, -7, 29, -1, 35, "no")
    fotos = [captura(conn, f"r/{k}.jpg", 11 * k, segundos=4 * k, direcao=10, lado="direito") for k in range(3)]
    i = imovel(conn, fotos[1])
    localizar(conn)
    o = obs(conn, i)
    assert o["edificacao_id"] == ne and o["localizacao_confianca"] == "media"
    assert o["localizacao_evidencias"]["excluidas_outro_lado"] == 1
    cand = conn.execute("SELECT edificacao_id, evidencias FROM candidato_edificacao WHERE imovel_id = %s", (i,)).fetchall()
    assert [c["edificacao_id"] for c in cand] == [ne] and cand[0]["evidencias"]["lado"] == "direito"


def test_varias_fotos_do_mesmo_imovel_e_varias_fachadas_numa_foto(conn):
    # Imóvel grande a leste (30 m de frente), visto em 4 fotos a cada 3 m; outra casa ao sul-leste na 1ª foto
    grande = caixa_m(conn, 10, -2, 25, 25, "grande")
    sul = caixa_m(conn, 6, -30, 30, -12, "sul")
    fotos = [captura(conn, f"v/{k}.jpg", 3 * k, segundos=2 * k, direcao=90) for k in range(4)]
    vistas = [imovel(conn, f) for f in fotos]
    a_sul = imovel(conn, fotos[0], bbox=(950, 0, 1000, 750))  # borda direita da 1ª foto: ~118°
    original = obs(conn, vistas[0])["bbox_detectada"], obs(conn, vistas[0])["bbox"]
    localizar(conn)

    assert {obs(conn, i)["edificacao_id"] for i in vistas} == {grande}
    assert obs(conn, a_sul)["edificacao_id"] == sul  # mesma foto, outra fachada, outra edificação
    grupo = obs(conn, vistas[0])["grupo_id"]
    assert grupo and all(obs(conn, i)["grupo_id"] == grupo for i in vistas) and obs(conn, a_sul)["grupo_id"] is None
    assert conn.execute("SELECT origem FROM grupo WHERE id = %s", (grupo,)).fetchone()["origem"] == "sugerido"

    # Revisor separa a 4ª: fica isolada mesmo depois de refazer as sugestões
    multivisual.separar(conn, vistas[3])
    localizar(conn)
    g = {i: obs(conn, i)["grupo_id"] for i in vistas}
    assert g[vistas[0]] == g[vistas[1]] == g[vistas[2]] != g[vistas[3]]
    # e depois junta de novo, com o grupo inteiro: vira grupo do revisor, que a sugestão não refaz
    gid, membros = multivisual.reunir(conn, [vistas[3], vistas[0]])
    assert membros == sorted(vistas)
    localizar(conn)
    assert {obs(conn, i)["grupo_id"] for i in vistas} == {gid}
    assert conn.execute("SELECT count(*) AS n FROM grupo").fetchone()["n"] == 1

    # Duas unidades independentes na mesma edificação
    for rotulo in ("loja térrea", "apartamento 1º andar"):
        conn.execute("INSERT INTO unidade (edificacao_id, grupo_id, rotulo) VALUES (%s, %s, %s)", (grande, gid, rotulo))
    assert conn.execute("SELECT count(*) AS n FROM unidade WHERE edificacao_id = %s", (grande,)).fetchone()["n"] == 2
    # Nada disso mexe nas observações nem nas caixas originais
    assert (obs(conn, vistas[0])["bbox_detectada"], obs(conn, vistas[0])["bbox"]) == original
    assert conn.execute("SELECT count(*) AS n FROM imovel").fetchone()["n"] == 5


def test_ler_enderecos_registra_e_nao_rele(conn, tmp_path, monkeypatch):
    from PIL import Image

    from geofat import enderecos

    monkeypatch.setattr(enderecos, "FOTOS_DIR", tmp_path)
    Image.new("RGB", (1000, 750)).save(tmp_path / "x.jpg")
    i = imovel(conn, captura(conn, "x.jpg"))
    conn.execute("""INSERT INTO referencia_publica (fonte, id_origem, logradouro, numero, geom)
                    VALUES ('cnefe', 'c1', 'RUA DO SOL', '37', ST_SetSRID(ST_MakePoint(%s, %s), 4326))""", (LON + 5 * M, LAT))
    chamadas = []

    def ler(jpeg):
        chamadas.append(len(jpeg))
        return "Nº 37\nFarmácia · Tel (98) 3232-1234"

    assert ler_enderecos(conn, ler, "ocr-falso") == (1, 1, [])
    l = conn.execute("SELECT * FROM leitura_endereco WHERE imovel_id = %s", (i,)).fetchone()
    assert (l["numero"], l["estado"], l["origem"], l["endereco_candidato"]) == ("37", "pendente", "ocr", "RUA DO SOL, 37")
    assert l["bbox"] == [400, 0, 600, 750] and l["evidencias"]["referencias"][0]["fonte"] == "cnefe"
    assert ler_enderecos(conn, ler, "ocr-falso") == (0, 0, []) and len(chamadas) == 1  # não relê a mesma caixa


def test_api_geo_correcoes_e_historico(conn, monkeypatch):
    from geofat import api, api_geo

    grande = caixa_m(conn, 10, -5, 25, 25, "grande")
    fotos = [captura(conn, f"api/{k}.jpg", 3 * k, segundos=2 * k, direcao=90) for k in range(2)]
    a, b = imovel(conn, fotos[0]), imovel(conn, fotos[1])
    conn.execute("""INSERT INTO referencia_publica (fonte, id_origem, logradouro, numero, geom)
                    VALUES ('cnefe', 'c1', 'RUA DO SOL', '218', ST_SetSRID(ST_MakePoint(%s, %s), 4326))""", (LON + 12 * M, LAT))
    localizar(conn)
    conn.commit()
    # A API abre as próprias conexões: aponta para o schema de teste
    teste = lambda: psycopg.connect(DATABASE_URL, row_factory=psycopg.rows.dict_row, options="-c search_path=teste,public")
    monkeypatch.setattr(api, "conectar", teste)
    monkeypatch.setattr(api_geo, "conectar", teste)
    c = TestClient(api.app)

    d = c.get(f"/api/imoveis/{a}").json()
    assert d["candidatas"][0]["edificacao_id"] == grande and d["grupo"]["origem"] == "sugerido"
    assert [m["id"] for m in d["grupo"]["membros"]] == [a, b]
    f = c.get(f"/api/edificacoes/{grande}").json()
    assert {o["id"] for o in f["observacoes"]} == {a, b} and f["lotes"] == []
    props = c.get(f"/api/edificacoes?bbox={LON - 0.001},{LAT - 0.001},{LON + 0.001},{LAT + 0.001}").json()["features"][0]["properties"]
    assert (props["associacao"], props["observacoes"]) == ("sugerida", 2)
    assert c.get("/api/trajetos").json()["features"][0]["properties"]["fotos"] == 2

    assert c.post(f"/api/imoveis/{b}/separar", json={}).status_code == 200
    assert c.get(f"/api/imoveis/{a}").json()["grupo"] is None  # grupo sugerido de 1 some
    g = c.post("/api/grupos", json={"imoveis": [a, b]}).json()
    assert c.get(f"/api/imoveis/{b}").json()["grupo"]["origem"] == "manual" and g["membros"] == [a, b]
    assert c.post("/api/grupos", json={"imoveis": [a, 999999]}).status_code == 404

    u = c.post(f"/api/edificacoes/{grande}/unidades", json={"rotulo": "loja", "categoria": "comercial", "grupo_id": g["id"]}).json()
    c.post(f"/api/edificacoes/{grande}/unidades", json={"rotulo": "casa"})
    assert c.delete(f"/api/unidades/{u['id']}").status_code == 200
    f = c.get(f"/api/edificacoes/{grande}").json()
    assert [x["rotulo"] for x in f["unidades"]] == ["casa"]
    assert [h["acao"] for h in f["historico"]] == ["unidade_removida", "unidade_criada", "unidade_criada"]

    lid = c.post(f"/api/imoveis/{a}/leituras", json={"numero": "0218"}).json()["id"]
    l = c.get(f"/api/imoveis/{a}").json()["leituras"][0]
    assert (l["numero"], l["estado"], l["endereco_candidato"], l["referencia_fonte"]) == ("218", "confirmada", "RUA DO SOL, 218", "cnefe")
    assert c.post(f"/api/imoveis/{a}/leituras", json={"numero": "S/N"}).status_code == 422
    assert c.post(f"/api/leituras/{lid}", json={"estado": "rejeitada"}).status_code == 200

    # Correção manual da edificação: vira 'manual' e não é desfeita ao recalcular
    assert c.post(f"/api/imoveis/{b}/localizacao", json={"edificacao_id": grande}).status_code == 200
    localizar(conn)
    assert obs(conn, b)["localizacao_fonte"] == "manual"
    acoes = [h["acao"] for h in c.get(f"/api/imoveis/{a}").json()["historico"]]
    assert {"grupo_reunir", "leitura_manual", "leitura_revisao"} <= set(acoes)
    assert "grupo_separar" in [h["acao"] for h in c.get(f"/api/imoveis/{b}").json()["historico"]]
