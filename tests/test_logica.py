import pytest
from PIL import Image

from geofat.classificar import interpretar
from geofat.detectar import filtrar_caixas
from geofat.edificacoes import token_s2
from geofat.fotos import fov_horizontal, ler_metadados
from geofat.localizar import azimute
from geofat.publicos import categoria_osm
from geofat.yolo_dataset import linhas_yolo


def test_interpretar_resposta_com_raciocinio_e_acento():
    texto = ('<think>parece {loja}</think> ```json\n{"categoria": "Institucional", "unidades": 0, "confianca": 1.7, '
             '"evidencias": "brasão", "limitacoes": "foto distante"}\n```')
    assert interpretar(texto) == ("institucional", 1, 1.0, "brasão", "foto distante")


def test_interpretar_categoria_copiada_do_prompt():
    # resposta real do Bonsai 2 9B: copiou a linha da lista de categorias
    texto = '{"categoria": "residencial: moradia (casa, prédio de apartamentos)", "unidades": 2, "confianca": 0.8}'
    assert interpretar(texto)[:2] == ("residencial", 2)


def test_interpretar_inconclusivo_sem_unidades():
    assert interpretar('{"categoria": "inconclusivo", "unidades": null}')[:2] == ("inconclusivo", None)


def test_interpretar_terreno_vazio():
    assert interpretar('{"categoria": "terreno_vazio", "unidades": 0}')[0] == "terreno_vazio"
    assert interpretar('{"categoria": "Terreno vazio: lote sem construção"}')[0] == "terreno_vazio"
    with pytest.raises(ValueError):
        interpretar('{"categoria": "terreno"}')


def test_interpretar_rejeita_categoria_desconhecida():
    with pytest.raises(ValueError):
        interpretar('{"categoria": "rural"}')
    with pytest.raises(ValueError):
        interpretar("não sei")


def test_gps_direcao_magnetica_e_rotacao(tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6  # foto de celular em pé: 90°
    exif[0x8825] = {1: "S", 2: (2.0, 31.0, 48.0), 3: "W", 4: (44.0, 18.0, 36.0), 16: "M", 17: 10.0}
    caminho = tmp_path / "f.jpg"
    Image.new("RGB", (40, 30)).save(caminho, exif=exif)
    m = ler_metadados(caminho)
    assert m["lat"] == pytest.approx(-2.53) and m["lon"] == pytest.approx(-44.31)
    assert m["direcao"] == pytest.approx(349.0)  # 10° magnético com declinação de -21°
    assert (m["largura"], m["altura"]) == (30, 40)


def test_fov_e_azimute():
    assert fov_horizontal(26, 4000, 3000) == pytest.approx(67.3, abs=0.2)  # celular, paisagem 4:3
    assert fov_horizontal(26, 3000, 4000) < fov_horizontal(26, 4000, 3000)  # retrato é mais estreito
    assert azimute(350, 60, 500, 1000) == pytest.approx(350)                # centro da foto
    assert azimute(350, 60, 1000, 1000) == pytest.approx(20)                # borda direita: +30°


def test_token_s2_confere_com_indice_do_open_buildings():
    # tokens do score_thresholds_s2_level_4.csv do Google; 07f7 = arquivo de nível 6 de São Luís
    assert token_s2(-10.0, -47.5, 4) == "933"
    assert token_s2(-2.53, -44.30, 4) == "07f"
    assert token_s2(-2.53, -44.30) == "07f7"


def test_categoria_osm():
    assert categoria_osm({"shop": "bakery", "building": "house"}) == "comercial"
    assert categoria_osm({"amenity": "townhall"}) == "institucional"
    assert categoria_osm({"amenity": "school"}) == "institucional"
    assert categoria_osm({"building": "apartments"}) == "residencial"
    assert categoria_osm({"amenity": "bench"}) is None


def test_filtrar_caixas_preserva_rotulo():
    assert filtrar_caixas([([0, 0, 10, 10], 0.5, "porta")]) == [([0, 0, 10, 10], 0.5, "porta")]


def test_filtrar_caixas_reais_do_dino():
    # caixas do Grounding DINO na foto do MHA (São Luís): a #12 engloba as duas fachadas
    mha = [([2309, 2, 3018, 1812], 0.52), ([18, 114, 1613, 1688], 0.37), ([9, 8, 3015, 1791], 0.35)]
    assert [b for b, _ in filtrar_caixas(mha)] == [[2309, 2, 3018, 1812], [18, 114, 1613, 1688]]
    duplicada = [([0, 0, 100, 100], 0.4), ([2, 1, 101, 99], 0.6)]
    assert filtrar_caixas(duplicada) == [([2, 1, 101, 99], 0.6)]
    assert filtrar_caixas([]) == []


def test_linhas_yolo_normalizadas():
    assert linhas_yolo([[10, 20, 30, 60]], 100, 200) == ["0 0.200000 0.200000 0.200000 0.200000"]
    assert linhas_yolo([[0, 0, 10, 10], [0, 0, 100, 200]], 100, 200, [2, 0])[0].startswith("2 ")


def test_numeros_de_endereco_no_texto_do_ocr():
    from geofat.enderecos import normalizar, numeros

    texto = "Rua do Sol, 218-A\nTel (98) 3232-1234 · Desde 1985 · R$ 12,50 · 14:30 · 25/12"
    assert numeros(texto) == ["218A", "1985"]
    assert numeros("NENHUM") == [] and numeros("") == []
    assert (normalizar("0218-a"), normalizar("S/N"), normalizar("0")) == ("218A", None, None)


def test_lado_da_via_pela_direcao_e_rumo():
    from geofat.trajetorias import lado

    assert (lado(90, 0), lado(270, 0), lado(10, 0), lado(180, 0), lado(0, 270)) == ("direito", "esquerdo", None, None, "direito")


def test_importar_exemplos_nome_e_percursos():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("importar_exemplos", Path(__file__).parent.parent / "ferramentas" / "importar_exemplos.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.ler_nome("direita_0034_lat-2_540666_lng-44_215725_jpg.rf.abc.jpg") == {"lado": "direito", "n": 34, "lat": -2.540666, "lon": -44.215725}
    assert m.ler_nome("esquerda_7_lat-2.5_lng-44.2.jpg")["lado"] == "esquerdo"
    assert m.ler_nome("foto.jpg") is None
    # duas saídas que recomeçam a numeração em ruas diferentes; a 1ª pula um quadro (0, 2, 3)
    q = lambda n, lat: {"lado": "direito", "n": n, "lat": lat, "lon": -44.2}
    quadros = [q(0, -2.5), q(2, -2.5 + 8 / 111320), q(3, -2.5 + 12.5 / 111320), q(0, -2.6), q(1, -2.6 + 4 / 111320)]
    t = m.percursos(quadros)
    assert t[0] == t[1] == t[2] != t[3] == t[4]


def test_prerotulagem_vereditos_e_decisao():
    from geofat.prerotulagem import CRITERIOS_PADRAO as C, decidir, ler_omitidas, proximo_limiar, veredito_caixa

    ok = veredito_caixa('<think>x</think>{"fachada": true, "problema": "nenhum", "confianca": 0.9}')
    assert ok["fachada"] and ok["problema"] == "nenhum"
    assert veredito_caixa('{"fachada": false}')["problema"] == "nao_e_fachada"
    assert veredito_caixa("não sei")["problema"] == "resposta_invalida"
    assert (ler_omitidas('{"sem_caixa": 2}'), ler_omitidas('{"sem_caixa": "x"}'), ler_omitidas("nada")) == (2, None, None)
    caixa, fraca = ([0, 0, 10, 10], 0.5), ([20, 0, 30, 10], 0.1)  # a 2ª tem score do detector abaixo do critério
    assert decidir([caixa], [ok], 0, C)[:2] == ("aceita", None)
    assert decidir([caixa], [ok], 1, C)[:2] == ("revisar", "omitidas")
    assert decidir([caixa, fraca], [ok, ok], 0, C)[:2] == ("revisar", "baixa_confianca")
    assert decidir([caixa], [veredito_caixa('{"fachada": false}')], 0, C)[:2] == ("rejeitada", "falsos_positivos")
    assert decidir([caixa], [ok], None, C)[:2] == ("revisar", "verificacao_incompleta")
    varias = veredito_caixa('{"fachada": true, "problema": "varias_fachadas", "confianca": 0.95}')
    assert decidir([caixa, caixa], [ok, varias], 3, C)[:2] == ("revisar", "falsos_positivos")  # sobe o limiar, não desce
    assert decidir([], [], 0, C)[:2] == ("rejeitada", "sem_caixas")
    assert proximo_limiar(0.35, "omitidas") == 0.27 and proximo_limiar(0.35, "falsos_positivos") == 0.43
    assert proximo_limiar(0.1, "omitidas") is None and proximo_limiar(0.35, "baixa_confianca") is None


def test_prerotulagem_tenta_de_novo_ate_o_limite():
    from PIL import Image

    from geofat.prerotulagem import CRITERIOS_PADRAO, pre_rotular_imagem

    limiares = []

    def detector(img, classes, limiar):
        limiares.append(limiar)
        return [([0, 0, 50, 50], 0.6, "fachada")]

    # o verificador sempre aponta uma fachada sem caixa: o laço para no máximo configurado
    perguntar = lambda jpeg, prompt: '{"fachada": true, "confianca": 0.9}' if "recorte" in prompt else '{"sem_caixa": 1}'
    cfg = {"limiar_inicial": 0.35, "max_tentativas": 3, "criterios": CRITERIOS_PADRAO}
    tentativas, melhor = pre_rotular_imagem(Image.new("RGB", (200, 100)), detector, perguntar, "building facade", cfg)
    assert limiares == [0.35, 0.27, 0.19] and len(tentativas) == 3
    assert melhor["decisao"] == "revisar" and melhor["problema"] == "omitidas"
    cfg["max_tentativas"] = 50  # mesmo configurado errado, não passa do teto
    limiares.clear()
    assert len(pre_rotular_imagem(Image.new("RGB", (200, 100)), detector, perguntar, "x", cfg)[0]) == 5
    assert limiares[-1] == 0.1


def test_prerotulagem_amostra_e_comparacao():
    import random

    from geofat.prerotulagem import comparar, sortear_amostra

    assert len(sortear_amostra(list(range(95)), 10, random.Random(1))) == 10 and sortear_amostra([7], 5) == [7]
    assert sortear_amostra([], 10) == []
    humano = [[0, 0, 100, 100], [200, 0, 300, 100]]
    assert comparar([[5, 5, 100, 100], [500, 0, 600, 100]], humano) == (1, 1, 1)


def test_mapillary_celulas_e_normalizar():
    from geofat.mapillary import AREA_MAX, celulas, normalizar, validar_bbox

    c = celulas((-44.31, -2.54, -44.30, -2.53))
    assert len(c) == 4 and all(abs(x[2] - x[0] - 0.005) < 1e-9 for x in c)
    with pytest.raises(ValueError, match="grande"):
        validar_bbox((-44.35, -2.60, -44.25, -2.50))
    with pytest.raises(ValueError, match="inválida"):
        validar_bbox((-44, -2, -45, -3))
    assert AREA_MAX <= 0.001
    n = normalizar({"id": 12, "geometry": {"coordinates": [-44.3, -2.53]}, "computed_geometry": {"coordinates": [-44.31, -2.531]},
                    "compass_angle": 10, "computed_compass_angle": 370.5, "captured_at": 1700000000000,
                    "creator": {"username": "ana"}, "sequence": "s1", "camera_type": "fisheye"})
    assert (n["id"], n["lon"], n["angulo"], n["autor"], n["pano"]) == ("12", -44.31, 10.5, "ana", False)
    assert normalizar({"id": 1, "geometry": {"coordinates": [0, 0]}, "camera_type": "spherical"})["pano"]


def test_mapillary_divide_area_densa(monkeypatch):
    import geofat.mapillary as M

    consultas = []

    def falso(caminho, token, bbox, **p):
        o, s, l, n = map(float, bbox.split(","))
        consultas.append(round(l - o, 6))
        if l - o > 0.003:  # célula densa: a API pede menos dados
            raise ValueError("Mapillary respondeu HTTP 500 (Please reduce the amount of data)")
        return {"data": [{"id": bbox, "geometry": {"coordinates": [o, s]}}]}

    monkeypatch.setattr(M, "_get", falso)
    assert len(M.buscar("t", (-44.305, -2.535, -44.300, -2.530))) == 4
    assert consultas == [0.005, 0.0025, 0.0025, 0.0025, 0.0025]


def test_prerotulagem_caixa_agrupada_sobe_limiar_e_poupa_chamada():
    from PIL import Image

    from geofat.prerotulagem import CRITERIOS_PADRAO, pre_rotular_imagem

    limiares, perguntas = [], []

    def detector(img, classes, limiar):
        limiares.append(limiar)
        return [([0, 0, 50, 50], 0.6, "fachada")] + ([([60, 0, 190, 90], 0.3, "fachada")] if limiar < 0.4 else [])

    def perguntar(jpeg, prompt):
        perguntas.append("recorte" in prompt)
        if "recorte" not in prompt:
            return '{"sem_caixa": 0}'
        import io
        largo = Image.open(io.BytesIO(jpeg)).width > 100  # a caixa larga engloba várias casas
        return '{"fachada": true, "problema": "varias_fachadas", "confianca": 0.9}' if largo else '{"fachada": true, "confianca": 0.9}'

    cfg = {"limiar_inicial": 0.35, "max_tentativas": 3, "criterios": CRITERIOS_PADRAO}
    tentativas, melhor = pre_rotular_imagem(Image.new("RGB", (200, 100)), detector, perguntar, "x", cfg)
    assert limiares == [0.35, 0.43] and melhor["decisao"] == "aceita"
    assert perguntas == [True, True, True, False]  # 1ª tentativa sem a pergunta das omitidas


def test_projecao_olho_de_peixe_do_mapillary():
    from geofat.localizar import angulo_da_coluna, coluna_do_angulo
    from geofat.mapillary import camera_de, fov_da_camera

    # Blackvue DR900M do Centro de São Luís, como o Mapillary informa
    cam = camera_de({"camera_type": "fisheye", "camera_parameters": [0.467681, -0.049097, 0.001897]})
    assert fov_da_camera(cam, 2048, 1152) == pytest.approx(130.4, abs=0.1)  # e não os 72° do padrão de celular
    for x in (0, 300, 1024, 1700, 2048):
        assert coluna_do_angulo(angulo_da_coluna(x, 2048, None, cam, 1152), 2048, None, cam, 1152) == pytest.approx(x, abs=0.01)
    # a 3/4 da largura o pinhole com o mesmo campo de visão erra mais de 10°
    assert angulo_da_coluna(1536, 2048, 130.4) - angulo_da_coluna(1536, 2048, None, cam, 1152) > 10
    assert camera_de({"camera_type": "spherical", "camera_parameters": [0.5, 0, 0]}) is None
    persp = camera_de({"camera_type": "perspective", "camera_parameters": [0.5, 0, 0]})
    assert fov_da_camera(persp, 400, 300) == pytest.approx(90)


def _xlsx(caminho, linhas):
    """Planilha mínima no formato do Excel: textos compartilhados, texto embutido e números."""
    import zipfile

    textos = sorted({v for r in linhas for v in r if isinstance(v, str) and v != "embutido"})
    def celula(c, v):
        if isinstance(v, str) and v == "embutido":
            return f'<c r="{c}" t="inlineStr"><is><t>{v}</t></is></c>'
        if isinstance(v, str):
            return f'<c r="{c}" t="s"><v>{textos.index(v)}</v></c>'
        return f'<c r="{c}"><v>{v}</v></c>'
    dados = "".join(f'<row r="{n}">' + "".join(celula(f"{chr(65 + k)}{n}", v) for k, v in enumerate(r) if v is not None)
                    + "</row>" for n, r in enumerate(linhas, 1))
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    with zipfile.ZipFile(caminho, "w") as z:
        z.writestr("xl/sharedStrings.xml", f"<sst {ns}>" + "".join(f"<si><t>{t}</t></si>" for t in textos) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData>{dados}</sheetData></worksheet>")


def test_planilha_le_so_o_que_localiza_o_imovel(tmp_path):
    from geofat.planilha import ler_alvos, selecionar

    cab = ["Nome Unidade", "Nome Localidade", "Categoria", "CPF ou CNPJ", "Matricula", "Valor em Aberto", "Coordenada X", "Coordenada Y"]
    _xlsx(tmp_path / "p.xlsx", [cab,
                                ["SAO LUIS", "CENTRO", "COMERCIAL", "CNPJ", 1230000, 201.48, -44.30, -2.53],
                                ["SAO LUIS", "VINHAIS", "RESIDENCIAL", "CPF", 1229800, 0, -44.22, -2.50],
                                ["SAO LUIS", "VINHAIS", "RESIDENCIAL", "CPF", 1229800, 0, -44.22, -2.50],  # repetida
                                ["SAO LUIS", "embutido", "RESIDENCIAL", "CPF", 77, 0, None, -2.50],         # sem coordenada
                                ["SAO LUIS", "CENTRO", "RESIDENCIAL", "CPF", 78, 0, 0, 0],                  # fora do Brasil
                                ["SAO LUIS", "COHAB", "RESIDENCIAL", "CPF", 79, 0, -2.55, -44.25]])          # X e Y trocados
    validos, invalidos = ler_alvos(tmp_path / "p.xlsx")
    assert invalidos == 2 and [v["matricula"] for v in validos] == ["1230000", "1229800", "79"]
    assert (validos[2]["lat"], validos[2]["lon"]) == (-2.55, -44.25)
    assert set(validos[0]) == {"linha", "matricula", "lat", "lon", "localidade", "municipio"}  # sem categoria, documento, débito
    assert validos[0]["localidade"] == "CENTRO" and validos[0]["lon"] == -44.30

    (tmp_path / "p.csv").write_text("matricula;latitude;longitude;categoria\n9;-2,5;-44,2;comercial\n", encoding="utf-8")
    assert ler_alvos(tmp_path / "p.csv")[0][0]["lat"] == -2.5
    (tmp_path / "ruim.csv").write_text("matricula;categoria\n9;comercial\n", encoding="utf-8")
    with pytest.raises(ValueError, match="lat, lon"):
        ler_alvos(tmp_path / "ruim.csv")

    muitos = [{"matricula": str(i), "localidade": "CENTRO" if i % 2 else "VINHAIS"} for i in range(40)]
    a, _ = selecionar(muitos, 3, semente=7)
    assert a == selecionar(muitos, 3, semente=7)[0] and len(a) == 3  # sorteio reprodutível
    assert all(x["localidade"] == "VINHAIS" for x in selecionar(muitos, 5, localidade="vinhais")[0])
    com, sem = selecionar(muitos, 2, cobertura=lambda x: int(x["matricula"]) % 3 == 0)
    assert len(com) == 2 and all(int(x["matricula"]) % 3 == 0 for x in com) and all(int(x["matricula"]) % 3 for x in sem)
    assert [x["matricula"] for x in selecionar(muitos, 9, matriculas=["5", "999", "2"])[0]] == ["5", "2"]
    assert len(selecionar(muitos, 30, cobertura=lambda x: 0, max_consultas=10)[1]) == 10  # teto de consultas à API


def test_direcionada_escolhe_fotos_e_cobre_colunas():
    from geofat.direcionada import _sobreposicao, escolher_fotos

    n = lambda i, lon: {"id": i, "lat": -2.5, "lon": lon}  # noqa: E731
    avaliadas = [("fora_do_campo", {"distancia_m": 5}, n(1, -44.3)), ("obstruido", {"distancia_m": 8}, n(2, -44.3001)),
                 ("no_campo", {"distancia_m": 20}, n(3, -44.3002)), ("no_campo", {"distancia_m": 21}, n(4, -44.30021)),
                 ("na_borda", {"distancia_m": 30}, n(5, -44.3006))]
    # foto fora do campo nunca entra; a 4 está a ~1 m da 3 (mesmo ponto de vista)
    assert [x[2]["id"] for x in escolher_fotos(avaliadas, 3)] == [3, 5, 2]
    assert _sobreposicao([0, 0, 100, 10], [0.25, 0.75], 400) == 0.0
    assert _sobreposicao([100, 0, 300, 10], [0.25, 0.5], 400) == 0.5
    assert _sobreposicao([100, 0, 300, 10], [0.4, 0.4], 400) == 1.0  # alvo sem polígono: um ponto dentro da caixa


def test_gpu_remota_tenta_de_novo_so_quando_vale(monkeypatch):
    import io
    import urllib.error

    import geofat.provedores as PV

    monkeypatch.setattr(PV, "ESPERAS_REMOTAS", (0, 0))
    falhas = [urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO()), urllib.error.URLError("timed out")]

    def instavel():
        if falhas:
            raise falhas.pop(0)
        return "ok"
    assert PV.com_retentativas(instavel, "t")() == "ok"
    chamadas = []

    def recusado():
        chamadas.append(1)
        raise urllib.error.HTTPError("u", 401, "x", {}, io.BytesIO())
    with pytest.raises(urllib.error.HTTPError):
        PV.com_retentativas(recusado, "t")()
    assert len(chamadas) == 1  # token errado não é repetido

    def fora():
        raise urllib.error.URLError(ConnectionRefusedError())
    with pytest.raises(PV.RemotoIndisponivel, match="3 tentativas"):
        PV.com_retentativas(fora, "t")()
    assert PV.OCORRENCIAS_REMOTAS[-1]["desistiu"]
    with pytest.raises(ValueError, match="sem token"):
        PV.chamar_remoto({"url": "http://x", "chave": None}, "/status")
