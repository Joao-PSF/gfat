"""Testa localização e comparação com o GSAN no PostGIS do docker compose, num schema descartável."""
import psycopg
import pytest

from geofat.config import RAIZ
from geofat.db import conectar, migrar
from geofat.gsan import importar_gsan
from geofat.localizar import localizar, posicionar_manual
from geofat.yolo_dataset import exportar_yolo

LAT = -2.53


@pytest.fixture
def conn():
    try:
        c = conectar()
    except psycopg.OperationalError:
        pytest.skip("PostGIS fora do ar (docker compose up -d)")
    c.execute("DROP SCHEMA IF EXISTS teste CASCADE; CREATE SCHEMA teste; SET search_path = teste, public")
    c.execute((RAIZ / "db" / "schema.sql").read_text(encoding="utf-8").replace("CREATE EXTENSION IF NOT EXISTS postgis;", ""))
    migrar(c)
    yield c
    c.rollback()
    c.execute("DROP SCHEMA teste CASCADE")
    c.commit()
    c.close()


def foto(conn, lon, direcao=None, lat=LAT):
    return conn.execute("""INSERT INTO foto (arquivo, fonte, largura, altura, direcao, fov_horizontal, geom)
                           VALUES (gen_random_uuid()::text, 'existente', 1000, 750, %s, 60,
                                   ST_SetSRID(ST_MakePoint(%s, %s), 4326)) RETURNING id""",
                        (direcao, lon, lat)).fetchone()["id"]


def imovel(conn, foto_id, categoria=None, unidades=None, bbox=(400, 0, 600, 750), **extra):
    """Imóvel na posição da câmera; `extra` são colunas adicionais (ex.: localizacao_confianca)."""
    colunas = "".join(f", {k}" for k in extra)
    return conn.execute(
        f"""INSERT INTO imovel (foto_id, bbox, geom, categoria_ia, unidades_ia{colunas})
            SELECT id, %s, geom, %s, %s{', %s' * len(extra)} FROM foto WHERE id = %s RETURNING id""",
        (list(bbox), categoria, unidades, *extra.values(), foto_id)).fetchone()["id"]


def edificacao(conn, lon1, lat1, lon2, lat2, id_origem="a"):
    return conn.execute("""INSERT INTO edificacao (fonte, id_origem, geom)
                           VALUES ('osm', %s, ST_MakeEnvelope(%s, %s, %s, %s, 4326)) RETURNING id""",
                        (id_origem, lon1, lat1, lon2, lat2)).fetchone()["id"]


def analise(conn, i):
    return conn.execute("SELECT * FROM imovel_analise WHERE id = %s", (i,)).fetchone()


def test_localizar_pela_direcao_da_camera(conn):
    # Câmera na rua olhando para o norte; casa ~11 m ao norte, outra casa a leste fora do campo
    norte = edificacao(conn, -44.30005, LAT + 0.0001, -44.29995, LAT + 0.0002, "norte")
    edificacao(conn, -44.2998, LAT - 0.00005, -44.2997, LAT + 0.00005, "leste")
    edificacao(conn, -44.30002, LAT - 0.00002, -44.29998, LAT + 0.00002, "contem_camera")  # erro de GPS
    com_direcao = imovel(conn, foto(conn, -44.3000, direcao=0))
    sem_direcao = imovel(conn, foto(conn, -44.3000))
    assert localizar(conn) == {"direcao": 1, "lado_via": 0, "camera": 1}

    a = analise(conn, com_direcao)
    assert (a["edificacao_id"], a["localizacao_fonte"], a["localizacao_confianca"]) == (norte, "direcao", "media")
    assert conn.execute("SELECT round(ST_Distance(geom::geography, geom_camera::geography)) d FROM imovel_analise "
                        "WHERE id = %s", (com_direcao,)).fetchone()["d"] == 11
    assert analise(conn, sem_direcao)["localizacao_confianca"] == "baixa"

    assert posicionar_manual(conn, sem_direcao, edificacao_id=norte)
    a = analise(conn, sem_direcao)
    assert (a["edificacao_id"], a["localizacao_fonte"], a["localizacao_confianca"]) == (norte, "manual", "alta")
    assert localizar(conn) == {"direcao": 1, "lado_via": 0, "camera": 0}  # posição manual não é sobrescrita


def test_situacoes_comerciais(conn, tmp_path):
    csv = tmp_path / "gsan.csv"
    csv.write_text(
        "matricula;latitude;longitude;endereco;economias_residencial;economias_comercial;economias_industrial;economias_publica\n"
        f"100;{LAT};-44.30000;Rua A;1;0;0;0\n"
        f"200;{LAT};-44.30100;Rua B;1;1;0;0\n"
        f"201;{LAT};-44.30101;Rua B ap 2;1;0;0;0\n"
        "300;;;sem coordenada;1;0;0;0\n", encoding="utf-8-sig")  # BOM, como o Excel salva
    assert importar_gsan(conn, csv) == (3, [5])
    assert conn.execute("SELECT categoria, economias FROM gsan_ligacao WHERE matricula = '200'").fetchone() == \
        {"categoria": "misto", "economias": 2}

    media = {"localizacao_fonte": "direcao", "localizacao_confianca": "media"}
    ids = {
        # câmera exatamente sobre a ligação 100, mas localização só pela câmera: não associa
        "associacao_inconclusiva": imovel(conn, foto(conn, -44.30000), "comercial", 1),
        "possivel_divergencia": imovel(conn, foto(conn, -44.30000), "comercial", 1, **media),
        # duas matrículas perto da fachada: economias somadas (3) e categoria combinada (misto)
        "ok": imovel(conn, foto(conn, -44.30100), "misto", 3, **media),
        "sem_correspondencia": imovel(conn, foto(conn, -44.29000), "residencial", 1, **media),
        "classificacao_inconclusiva": imovel(conn, foto(conn, -44.30000), "inconclusivo", None, **media),
        "nao_classificado": imovel(conn, foto(conn, -44.30000), **media),
        "descartado": imovel(conn, foto(conn, -44.30000), "comercial", 1, fachada_valida=False),
    }
    for esperado, i in ids.items():
        assert analise(conn, i)["situacao"] == esperado, esperado
        if esperado != "possivel_divergencia":
            assert analise(conn, i)["tipo_divergencia"] is None, esperado
    a = analise(conn, ids["ok"])
    assert (a["matriculas"], a["economias_gsan"], a["categoria_gsan"], a["associacao"]) == \
        (["200", "201"], 3, "misto", "sugerida")

    # Revisão confirma classificação e associação: possível -> confirmada
    i = ids["possivel_divergencia"]
    conn.execute("UPDATE imovel SET revisado_em = now(), matriculas_confirmadas = '{100}' WHERE id = %s", (i,))
    a = analise(conn, i)
    assert (a["situacao"], a["tipo_divergencia"], a["associacao"]) == ("divergencia_confirmada", "categoria", "confirmada")

    # Associação confirmada vale mesmo com localização baixa; lista vazia = sem matrícula confirmada
    i = ids["associacao_inconclusiva"]
    conn.execute("UPDATE imovel SET revisado_em = now(), matriculas_confirmadas = '{}' WHERE id = %s", (i,))
    assert analise(conn, i)["situacao"] == "sem_matricula_confirmada"


def test_classificar_grava_e_descarta_se_caixa_mudou(conn, tmp_path, monkeypatch):
    from PIL import Image

    import geofat.classificar as c

    f = foto(conn, -44.3)
    arquivo = conn.execute("UPDATE foto SET arquivo = 'f.jpg' WHERE id = %s RETURNING arquivo", (f,)).fetchone()["arquivo"]
    Image.new("RGB", (1000, 750)).save(tmp_path / arquivo)
    estavel, redesenhada = imovel(conn, f), imovel(conn, f)
    conn.commit()

    def bonsai_de_teste(jpeg):  # simula o revisor redesenhando a 2ª caixa enquanto o modelo responde
        conn.execute("UPDATE imovel SET bbox = '{1,1,50,50}' WHERE id = %s", (redesenhada,))
        return '{"categoria": "comercial", "unidades": 2, "confianca": 0.7, "evidencias": "letreiro"}'

    monkeypatch.setattr(c, "FOTOS_DIR", tmp_path)
    ok, falhas = c.classificar(conn, perguntar=lambda jpeg, prompt: bonsai_de_teste(jpeg), nome_modelo="teste")
    assert ok == 1 and len(falhas) == 1 and "caixa alterada" in falhas[0]
    assert analise(conn, estavel)["categoria_ia"] == "comercial"
    assert analise(conn, redesenhada)["categoria_ia"] is None


def test_exportar_yolo_so_fotos_revisadas(conn, tmp_path):
    from PIL import Image

    f = foto(conn, -44.3)
    imovel(conn, f, bbox=(100, 150, 300, 450), fachada_valida=True)
    imovel(conn, foto(conn, -44.3))  # sem revisão: fica de fora
    arquivo = conn.execute("SELECT arquivo FROM foto WHERE id = %s", (f,)).fetchone()["arquivo"]
    Image.new("RGB", (1000, 750)).save(tmp_path / f"{arquivo}.jpg")
    conn.execute("UPDATE foto SET arquivo = arquivo || '.jpg'")

    assert exportar_yolo(conn, tmp_path / "ds", raiz=tmp_path) == 1
    rotulos = list((tmp_path / "ds" / "labels").rglob("*.txt"))
    assert [r.read_text() for r in rotulos] == ["0 0.200000 0.400000 0.200000 0.400000"]


def test_fila_de_tarefas(conn):
    from geofat import tarefas as T

    t = T.enfileirar(conn, "localizar", "Localizar fachadas")
    T.cancelar(conn, t)  # na fila: cancela na hora
    assert conn.execute("SELECT estado FROM tarefa WHERE id = %s", (t,)).fetchone()["estado"] == "cancelada"
    nova = T.repetir(conn, t)["id"]
    assert conn.execute("SELECT estado, repete FROM tarefa WHERE id = %s", (nova,)).fetchone() == {"estado": "fila", "repete": t}
    conn.execute("UPDATE tarefa SET estado = 'executando' WHERE id = %s", (nova,))
    T.cancelar(conn, nova)  # executando: só pede a parada
    assert conn.execute("SELECT estado, cancelar FROM tarefa WHERE id = %s", (nova,)).fetchone() == \
        {"estado": "executando", "cancelar": True}
    assert T.repetir(conn, nova) is None  # só falha ou cancelada pode ser repetida


def test_etapas_respeitam_capacidades(conn):
    from geofat.provedores import disponibilidade, modelo_da_etapa

    assert modelo_da_etapa(conn, "classificacao")["tipo"] == "vlm"
    assert modelo_da_etapa(conn, "ocr", obrigatorio=False) is None  # OCR começa desligado
    dino = modelo_da_etapa(conn, "deteccao")
    conn.execute("UPDATE etapa SET modelo_id = %s WHERE etapa = 'classificacao'", (dino["id"],))
    with pytest.raises(ValueError, match="não faz"):
        modelo_da_etapa(conn, "classificacao")
    yolo = conn.execute("""INSERT INTO modelo (nome, tipo, config) VALUES ('y', 'yolo', '{"pesos": "nao/existe.pt"}')
                           RETURNING *""").fetchone()
    assert disponibilidade({**yolo, "provedor_id": None})[0] == "nao_instalado"


def test_dataset_anotacao_aprovacao_e_exportacao(conn, tmp_path, monkeypatch):
    import io

    from PIL import Image

    import geofat.datasets as D

    monkeypatch.setattr(D, "FOTOS_DIR", tmp_path)
    ds = D.criar(conn, "teste", ["fachada", "porta"])
    exif = Image.Exif()
    exif[0x8825] = {1: "S", 2: (2.0, 31.0, 48.0), 3: "W", 4: (44.0, 18.0, 36.0)}
    jpeg = io.BytesIO()
    Image.new("RGB", (200, 100)).save(jpeg, "JPEG", exif=exif)
    a = D.adicionar_upload(conn, ds, "celular.jpg", jpeg.getvalue())
    b = D.adicionar_upload(conn, ds, "b.jpg", jpeg.getvalue())
    arquivo = conn.execute("SELECT arquivo FROM dataset_imagem WHERE id = %s", (a,)).fetchone()["arquivo"]
    assert 0x8825 not in Image.open(tmp_path / arquivo).getexif()  # o GPS da foto enviada não fica guardado

    porta = {"classe": "porta", "bbox": [10, 10, 50, 90], "origem": "manual"}
    with pytest.raises(ValueError, match="não existe"):
        D.salvar_anotacoes(conn, a, [{**porta, "classe": "janela"}])
    with pytest.raises(ValueError, match="fora da imagem"):
        D.salvar_anotacoes(conn, a, [{**porta, "bbox": [10, 10, 250, 90]}])
    with pytest.raises(ValueError, match="sugestões"):
        D.salvar_anotacoes(conn, a, [{**porta, "origem": "sugestao"}], aprovar=True)
    assert D.salvar_anotacoes(conn, a, [porta, {"classe": "fachada", "bbox": [0, 0, 200, 100], "origem": "aceita"}]) == "revisar"
    assert D.salvar_anotacoes(conn, a, [porta], aprovar=True) == "aprovada"
    assert D.salvar_anotacoes(conn, b, [], aprovar=True) == "aprovada"  # imagem de fundo

    with pytest.raises(ValueError, match="validação"):
        D.exportar(conn, ds, tmp_path / "yolo")
    conn.execute("UPDATE dataset_imagem SET conjunto = 'validacao' WHERE id = %s", (b,))
    assert D.exportar(conn, ds, tmp_path / "yolo") == {"treino": 1, "validacao": 1, "teste": 0}
    assert (tmp_path / "yolo" / "labels" / "train" / f"{a}.txt").read_text() == "1 0.150000 0.500000 0.200000 0.800000"
    assert (tmp_path / "yolo" / "labels" / "val" / f"{b}.txt").read_text() == ""
    assert "  0: fachada\n  1: porta\n" in (tmp_path / "yolo" / "data.yaml").read_text()


def test_mapillary_e_prerotulagem(conn, tmp_path, monkeypatch):
    import io

    from PIL import Image
    from psycopg.types.json import Jsonb

    import geofat.datasets as D
    import geofat.mapillary as MP
    import geofat.prerotulagem as PR
    import geofat.provedores as PV

    for m in (D, MP, PR):
        monkeypatch.setattr(m, "FOTOS_DIR", tmp_path)
    img = {"geometry": {"coordinates": [-44.3, LAT]}, "compass_angle": 90, "creator": {"username": "ana"}, "sequence": "s1"}
    resposta = {"data": [{**img, "id": 1, "captured_at": 1_700_000_000_000, "thumb_2048_url": "u1"},
                         {**img, "id": 2, "captured_at": 1_700_000_001_000, "thumb_2048_url": "u2"},
                         {**img, "id": 3, "captured_at": 1_700_000_002_000, "thumb_2048_url": "u3"},
                         {**img, "id": 9, "is_pano": True, "thumb_2048_url": "u9"}]}
    monkeypatch.setattr(MP, "_get", lambda caminho, token, **p: resposta)
    jpeg = io.BytesIO()
    Image.new("RGB", (400, 200)).save(jpeg, "JPEG")
    ds = D.criar(conn, "mly", ["fachada"])
    r = MP.importar(conn, "token", (-44.31, -2.54, -44.29, -2.52), 10, ds, baixar=lambda url: jpeg.getvalue())
    assert (r["novas"], r["adicionadas"]) == (3, 3)  # a panorâmica fica de fora
    assert MP.importar(conn, "token", (-44.31, -2.54, -44.29, -2.52), 10, ds, baixar=lambda url: 1 / 0)["novas"] == 0
    f = conn.execute("""SELECT f.*, p.licenca, p.autor FROM foto f JOIN foto_procedencia p ON p.foto_id = f.id
                        WHERE p.id_externo = '2'""").fetchone()
    assert (f["fonte"], f["trajeto"], f["sequencia"], f["direcao"], f["licenca"]) == ("mapillary", "mapillary-s1", 1, 90, "CC BY-SA 4.0")
    assert (tmp_path / f["arquivo"]).read_bytes() == jpeg.getvalue()  # original guardado sem recodificar

    # detector acha 1 fachada com o limiar inicial e 2 com o limiar menor; o verificador aponta 1 faltando na 1ª
    monkeypatch.setattr(PV, "obter_detector", lambda m: lambda im, classes, limiar: [([10, 10, 100, 190], 0.6, "fachada")]
                        + ([([200, 10, 390, 190], 0.5, "fachada")] if limiar < 0.3 else []))
    omissoes = []

    def verificador(jpeg, prompt):
        if "recorte" in prompt:
            return '{"fachada": true, "confianca": 0.9}'
        omissoes.append(1)
        return '{"sem_caixa": %d}' % (len(omissoes) == 1)

    monkeypatch.setattr(PV, "obter_verificador", lambda m: verificador)
    imagens = [x["id"] for x in conn.execute("SELECT id FROM dataset_imagem WHERE dataset_id = %s ORDER BY id", (ds,))]
    amostra = imagens[0]
    conn.execute("UPDATE dataset_imagem SET amostra_avaliacao = true WHERE id = %s", (amostra,))
    det = conn.execute("SELECT id FROM modelo WHERE tipo = 'grounding_dino'").fetchone()["id"]
    ver = conn.execute("SELECT id FROM modelo WHERE tipo = 'vlm'").fetchone()["id"]
    cfg = {"detector_id": det, "verificador_id": ver, "max_tentativas": 3, "criterios": PR.CRITERIOS_PADRAO,
           "revisao_humana": False, "limiar_inicial": None}
    p = conn.execute("INSERT INTO prerotulagem (dataset_id, config, imagem_ids) VALUES (%s, %s, %s) RETURNING id",
                     (ds, Jsonb(cfg), imagens)).fetchone()["id"]
    from geofat.tarefas import ContextoConsole

    assert PR.executar(conn, ContextoConsole(), p)["aceita"] == 3
    assert conn.execute("SELECT count(*) AS n FROM verificacao WHERE prerotulagem_id = %s", (p,)).fetchone()["n"] == 4  # 1ª imagem: 2 tentativas
    estados = {x["id"]: (x["estado"], x["aprovacao"], x["n"]) for x in conn.execute(
        """SELECT i.id, i.estado, i.aprovacao, count(a.id) AS n FROM dataset_imagem i LEFT JOIN anotacao a ON a.imagem_id = i.id
           WHERE i.dataset_id = %s GROUP BY i.id""", (ds,))}
    assert estados[amostra] == ("revisar", None, 0)  # amostra sem sugestões, para a pessoa anotar sem influência
    assert estados[imagens[1]] == ("aprovada", "automatica", 1)
    assert PR.executar(conn, ContextoConsole(), p)["aceita"] == 0  # retomar não refaz o que já foi feito

    D.salvar_anotacoes(conn, amostra, [{"classe": "fachada", "bbox": [12, 10, 100, 190], "origem": "manual"}], aprovar=True)
    r = PR.resumo(conn, p)
    assert (r["amostra_revisada"], r["precisao"], r["revocacao"], r["aceitas_sem_correcao"]) == (1, 0.5, 1.0, 0)
    assert conn.execute("SELECT count(*) AS n FROM anotacao_versao WHERE imagem_id = %s", (amostra,)).fetchone()["n"] == 2

    conn.execute("UPDATE dataset_imagem SET conjunto = 'validacao' WHERE id = %s", (imagens[2],))
    D.exportar(conn, ds, tmp_path / "yolo")
    assert "© ana\tCC BY-SA 4.0" in (tmp_path / "yolo" / "ATRIBUICAO.txt").read_text(encoding="utf-8")


def test_analise_direcionada_e_gpu_remota(conn, tmp_path, monkeypatch):
    import functools
    import io

    from PIL import Image

    import geofat.classificar as CL
    import geofat.direcionada as DI
    import geofat.edificacoes as ED
    import geofat.mapillary as MP
    import geofat.provedores as PV
    from geofat.prerotulagem import PROMPT_CAIXA
    from geofat.tarefas import ContextoConsole

    for m in (DI, MP):
        monkeypatch.setattr(m, "FOTOS_DIR", tmp_path)
    monkeypatch.setattr(ED, "importar_open_buildings", lambda conn, area: 0)  # sem rede no teste
    monkeypatch.setattr(ED, "importar_osm", lambda conn, area: 0)
    conn.execute("UPDATE provedor SET chave = 'tok' WHERE tipo = 'mapillary'")
    # alvo dentro de uma casa ~13 m ao norte da câmera; vizinha a leste
    casa = edificacao(conn, -44.30006, LAT + 0.00008, -44.29994, LAT + 0.00016, "casa")
    edificacao(conn, -44.29990, LAT + 0.00008, -44.29978, LAT + 0.00016, "vizinha")
    alvo = conn.execute("""INSERT INTO alvo (matricula, origem, geom) VALUES ('123', 'teste.xlsx',
                           ST_SetSRID(ST_MakePoint(-44.3, %s), 4326)) RETURNING id""", (LAT + 0.00012,)).fetchone()["id"]

    def imagem(i, lat, angulo):
        return {"id": i, "geometry": {"coordinates": [-44.3, lat]}, "compass_angle": angulo, "captured_at": 1_700_000_000_000 + i,
                "sequence": "s", "creator": {"username": "ana"}, "camera_type": "perspective",
                "camera_parameters": [0.5, 0, 0], "width": 400, "height": 200, "thumb_2048_url": f"u{i}"}
    resposta = {"data": [imagem(1, LAT, 0), imagem(2, LAT - 0.00005, 180), imagem(3, LAT - 0.0008, 0)]}
    monkeypatch.setattr(MP, "_get", lambda caminho, token, **p: resposta)
    jpeg = io.BytesIO()
    Image.new("RGB", (400, 200)).save(jpeg, "JPEG")
    monkeypatch.setattr(MP, "baixar_imagens", functools.partial(MP.baixar_imagens, baixar=lambda url: jpeg.getvalue()))
    obter_detector, obter_classificador = PV.obter_detector, PV.obter_classificador
    caixas = [([150, 20, 250, 180], 0.6, "fachada"), ([330, 20, 400, 180], 0.5, "fachada")]  # centro: a casa; borda: a vizinha
    monkeypatch.setattr(PV, "obter_detector", lambda m: lambda img: caixas)
    perguntas = []

    def perguntar(jpeg, prompt):
        perguntas.append(prompt == PROMPT_CAIXA)
        return ('{"fachada": true, "problema": "nenhum", "confianca": 0.9}' if prompt == PROMPT_CAIXA else
                '{"categoria": "comercial", "unidades": 1, "confianca": 0.8, "evidencias": "letreiro", "limitacoes": ""}')
    monkeypatch.setattr(PV, "obter_classificador", lambda m: (perguntar, "vlm-falso"))

    cfg = {"fotos_por_alvo": 3, "raio_m": 60, "verificar": True}
    a1, _ = DI.criar_analise(conn, [alvo], cfg)
    DI.executar(conn, ContextoConsole(), a1)
    x = conn.execute("SELECT * FROM analise_alvo WHERE analise_id = %s", (a1,)).fetchone()
    assert x["estado"] == "concluido" and x["edificacao_id"] == casa and x["edificacao_evidencias"]["contem_coordenada"]
    assert x["resultado"]["situacoes"] == {"no_campo": 1, "fora_do_campo": 1, "longe": 1}  # só a 1ª foto é baixada
    assert x["resultado"]["situacao"] == "observado_provavel" and x["buscadas"] == 3
    f = conn.execute("SELECT * FROM analise_foto WHERE analise_id = %s", (a1,)).fetchone()
    avaliadas = f["deteccao"]["caixas_avaliadas"]
    assert [c["associacao"] for c in avaliadas] == ["provavel", "possivel"] and f["bbox"] == [150, 20, 250, 180]
    assert f["classificacao"]["categoria"] == "comercial" and perguntas == [True, False]  # só a caixa do alvo vai ao modelo
    assert conn.execute("SELECT categoria_ia FROM imovel WHERE id = %s", (f["imovel_id"],)).fetchone()["categoria_ia"] == "comercial"
    assert conn.execute("SELECT count(*) AS n FROM foto WHERE fonte = 'mapillary'").fetchone()["n"] == 1
    assert DI.executar(conn, ContextoConsole(), a1) and perguntas == [True, False]  # retomar não refaz nada
    assert DI.criar_analise(conn, [alvo], cfg) == (None, [alvo])  # já analisado neste ambiente

    # nova execução com outro detector (só a caixa da borda): a observação já classificada não muda
    a2, _ = DI.criar_analise(conn, [alvo], cfg, reprocessar=True)
    caixas[:] = [([330, 20, 400, 180], 0.5, "fachada")]
    DI.executar(conn, ContextoConsole(), a2)
    c = DI.comparar(conn, a1, a2)
    assert (c["fotos"][0]["caixas_a"], c["fotos"][0]["caixas_b"], c["fotos"][0]["caixas_coincidentes"]) == (2, 1, 1)
    assert (c["fotos"][0]["associacao_a"], c["fotos"][0]["associacao_b"]) == ("provavel", "possivel")
    assert conn.execute("SELECT count(*) AS n FROM imovel").fetchone()["n"] == 2
    assert DI.medicoes(conn)["local"]["fotos"] == 2 and DI.estimar(conn, a2) == 0

    # outro alvo na mesma casa: a mesma foto não passa de novo pelo detector nem pelo modelo
    alvo2 = conn.execute("""INSERT INTO alvo (matricula, origem, geom) VALUES ('124', 'teste.xlsx',
                            ST_SetSRID(ST_MakePoint(-44.29999, %s), 4326)) RETURNING id""", (LAT + 0.00012,)).fetchone()["id"]
    n = len(perguntas)
    a5, _ = DI.criar_analise(conn, [alvo2], cfg)
    DI.executar(conn, ContextoConsole(), a5)
    f5 = conn.execute("SELECT deteccao, classificacao FROM analise_foto WHERE analise_id = %s", (a5,)).fetchone()
    assert f5["deteccao"]["reaproveitada"] and f5["deteccao"]["modelo_reaproveitado"] and f5["classificacao"]["categoria"] == "comercial"
    assert len(perguntas) == n

    # limites: tempo máximo e ambiente trocado não enviam trabalho novo
    a3, _ = DI.criar_analise(conn, [alvo], {**cfg, "tempo_max_min": 1}, reprocessar=True)
    conn.execute("UPDATE analise SET gasto_s = 61 WHERE id = %s", (a3,))
    conn.commit()  # como a API faz antes de enfileirar: executar() desfaz o que não terminou
    with pytest.raises(DI.LimiteAtingido, match="tempo máximo"):
        DI.executar(conn, ContextoConsole(), a3)
    conn.execute("""UPDATE configuracao SET valor = '{"ambiente": "runpod", "custo_hora_usd": 2, "orcamento_usd": 0.01}'""")
    conn.commit()
    with pytest.raises(ValueError, match="ambiente local"):
        DI.executar(conn, ContextoConsole(), a3)
    a4, _ = DI.criar_analise(conn, [alvo], cfg, reprocessar=True)
    conn.execute("UPDATE analise SET gasto_s = 60 WHERE id = %s", (a4,))  # 1 min a US$ 2/h > US$ 0,01
    conn.commit()
    with pytest.raises(DI.LimiteAtingido, match="orçamento"):
        DI.executar(conn, ContextoConsole(), a4)

    # ambiente Runpod: DINO e Bonsai vão para o serviço GPU, só com a imagem e os parâmetros
    conn.execute("UPDATE provedor SET url = 'https://pod.exemplo', chave = 'segredo' WHERE tipo = 'gpu_remota'")
    enviados = []
    monkeypatch.setattr(PV, "chamar_remoto", lambda r, caminho, corpo=None, timeout=300: enviados.append((r, caminho, corpo))
                        or {"caixas": [[[1, 2, 30, 40], 0.9, "fachada"]]})
    m = PV.modelo_da_etapa(conn, "deteccao")
    assert m["remoto"] == {"url": "https://pod.exemplo", "chave": "segredo"}
    assert obter_detector(m)(Image.new("RGB", (64, 48))) == [([1, 2, 30, 40], 0.9, "fachada")]
    assert enviados[0][1] == "/detectar" and set(enviados[0][2]) == {"imagem", "modelo", "classes", "limiar"}
    usados = []
    monkeypatch.setattr(PV, "modelos_servidos", lambda url, chave=None: [m2["config"]["modelo"]])
    monkeypatch.setattr(CL, "perguntar_vlm", lambda jpeg, url, modelo, prompt, parametros, chave: usados.append((url, chave)) or "{}")
    m2 = PV.modelo_da_etapa(conn, "classificacao")
    obter_classificador(m2)[0](b"jpeg", "p")
    assert usados == [("https://pod.exemplo/v1", "segredo")]
    conn.execute("UPDATE configuracao SET valor = '{\"ambiente\": \"local\"}'")
    assert "remoto" not in PV.modelo_da_etapa(conn, "deteccao")  # local continua funcionando com o pod desligado
