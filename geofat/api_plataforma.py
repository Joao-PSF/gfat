"""Rotas da plataforma: análise comercial, modelos de IA, datasets, treinos e tarefas em segundo plano."""
import io
import re
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from . import tarefas as T
from .config import RAIZ
from .db import conectar, registrar

rotas = APIRouter(prefix="/api")
DATA = RAIZ / "data"


def _api():
    from . import api

    return api


# ---------------- Tarefas ----------------

COLUNAS_TAREFA = """id, tipo, fila, titulo, parametros, estado, atual, total, unidade, mensagem, resultado, arquivo,
                    cancelar, repete, criada_em, iniciada_em, terminada_em"""


@rotas.get("/tarefas")
def listar_tarefas(limite: int = Query(80, le=500), ativas: bool = False, tipo: str | None = None):
    with conectar() as conn:
        return conn.execute(f"""SELECT {COLUNAS_TAREFA}, jsonb_path_query_array(log, '$[last-7 to last]') AS log
                                FROM tarefa WHERE (NOT %(ativas)s OR estado IN ('fila', 'executando'))
                                  AND (%(tipo)s::text IS NULL OR tipo = %(tipo)s)
                                ORDER BY id DESC LIMIT %(limite)s""",
                            {"ativas": ativas, "tipo": tipo, "limite": limite}).fetchall()


@rotas.get("/tarefas/{tarefa_id}")
def ver_tarefa(tarefa_id: int):
    with conectar() as conn:
        t = conn.execute(f"SELECT {COLUNAS_TAREFA}, log FROM tarefa WHERE id = %s", (tarefa_id,)).fetchone()
    if not t:
        raise HTTPException(404, "tarefa não encontrada")
    return t


class NovaTarefa(BaseModel):
    tipo: Literal["detectar", "classificar", "localizar", "ler_enderecos", "importar_fotos", "importar_edificacoes"]
    limite: int | None = Field(None, ge=1, le=100000)
    pasta: str | None = Field(None, max_length=200, pattern=r"^[\w\- ./]*$")
    fonte: str | None = Field(None, pattern=r"^(osm|open_buildings|existente|captura_propria)$")


TITULOS = {"detectar": "Detectar fachadas", "classificar": "Classificar imóveis", "localizar": "Localizar fachadas",
           "ler_enderecos": "Ler números de endereço",
           "importar_fotos": "Importar fotos", "importar_edificacoes": "Importar edificações"}


@rotas.post("/tarefas")
def criar_tarefa(n: NovaTarefa):
    if n.pasta and ".." in n.pasta:
        raise HTTPException(422, "pasta inválida")
    params = {"detectar": {"limite": n.limite}, "classificar": {"limite": n.limite}, "localizar": {},
              "ler_enderecos": {"limite": n.limite},
              "importar_fotos": {"pasta": n.pasta or None, "fonte": n.fonte or "existente"},
              "importar_edificacoes": {"fonte": n.fonte or "osm"}}[n.tipo]
    with conectar() as conn:
        return {"id": T.enfileirar(conn, n.tipo, TITULOS[n.tipo], **params)}


@rotas.post("/tarefas/{tarefa_id}/cancelar")
def cancelar_tarefa(tarefa_id: int):
    with conectar() as conn:
        T.cancelar(conn, tarefa_id)
    return {"ok": True}


@rotas.post("/tarefas/{tarefa_id}/repetir")
def repetir_tarefa(tarefa_id: int):
    with conectar() as conn:
        nova = T.repetir(conn, tarefa_id)
    if not nova:
        raise HTTPException(409, "só tarefas com falha ou canceladas podem ser repetidas")
    return nova


@rotas.get("/tarefas/{tarefa_id}/arquivo")
def baixar_resultado(tarefa_id: int):
    with conectar() as conn:
        t = conn.execute("SELECT arquivo FROM tarefa WHERE id = %s AND estado = 'concluida'", (tarefa_id,)).fetchone()
    caminho = (DATA / t["arquivo"]).resolve() if t and t["arquivo"] else None
    if not caminho or not caminho.is_relative_to(DATA.resolve()) or not caminho.exists():
        raise HTTPException(404, "arquivo não disponível")
    return FileResponse(caminho, filename=caminho.name)


# ---------------- Análise comercial ----------------

@rotas.get("/comercial")
def comercial():
    """Imóveis para a análise comercial. O GSAN de um imóvel só aparece depois da revisão da classificação."""
    with conectar() as conn:
        linhas = conn.execute("""
            SELECT a.id, a.foto_id, a.situacao, a.associacao,
                   CASE WHEN a.revisado_em IS NOT NULL THEN a.tipo_divergencia END AS tipo_divergencia, a.categoria_final, a.unidades_final,
                   a.confianca_ia, a.localizacao_confianca, a.revisado_em IS NOT NULL AS revisado,
                   CASE WHEN a.revisado_em IS NOT NULL THEN a.matriculas END AS matriculas,
                   CASE WHEN a.revisado_em IS NOT NULL THEN a.categoria_gsan END AS categoria_gsan,
                   CASE WHEN a.revisado_em IS NOT NULL THEN a.economias_gsan END AS economias_gsan,
                   CASE WHEN a.revisado_em IS NOT NULL THEN
                     (SELECT string_agg(endereco, ' / ') FROM gsan_ligacao WHERE matricula = ANY (a.matriculas)) END AS endereco,
                   d.decisao, d.observacao AS observacao_decisao, d.decidida_em
            FROM imovel_analise a LEFT JOIN decisao_comercial d ON d.imovel_id = a.id
            WHERE a.situacao IN ('divergencia_confirmada', 'possivel_divergencia', 'sem_correspondencia',
                                 'sem_matricula_confirmada', 'associacao_inconclusiva')
            ORDER BY a.id""").fetchall()
        n = conn.execute("SELECT count(*) AS n FROM gsan_ligacao").fetchone()["n"]
        quando = conn.execute("SELECT max(em) AS em FROM historico WHERE entidade = 'gsan' AND acao = 'importacao'").fetchone()["em"]
    return {"gsan_ligacoes": n, "gsan_importado_em": quando, "imoveis": linhas if n else []}


class Decisao(BaseModel):
    decisao: Literal["confirmada", "vistoria", "descartada"] | None
    observacao: str | None = Field(None, max_length=1000)


@rotas.post("/imoveis/{imovel_id}/decisao")
def decidir(imovel_id: int, d: Decisao):
    """Decisão do analista sobre a divergência. Fica no GeoFAT e sai no relatório; o GSAN não é alterado."""
    with conectar() as conn:
        a = conn.execute("SELECT situacao FROM imovel_analise WHERE id = %s", (imovel_id,)).fetchone()
        if not a:
            raise HTTPException(404, "imóvel não encontrado")
        if d.decisao == "confirmada" and a["situacao"] not in ("divergencia_confirmada", "sem_matricula_confirmada"):
            raise HTTPException(409, "para confirmar, revise a classificação e confirme as matrículas do imóvel")
        if d.decisao is None:
            conn.execute("DELETE FROM decisao_comercial WHERE imovel_id = %s", (imovel_id,))
        else:
            conn.execute("""INSERT INTO decisao_comercial (imovel_id, decisao, observacao) VALUES (%s, %s, %s)
                            ON CONFLICT (imovel_id) DO UPDATE SET decisao = excluded.decisao,
                              observacao = excluded.observacao, decidida_em = now()""",
                         (imovel_id, d.decisao, d.observacao))
        registrar(conn, "imovel", imovel_id, "decisao", d.model_dump())
    return {"ok": True}


@rotas.get("/comercial/relatorio.csv")
def relatorio_csv(ids: str | None = Query(None, pattern=r"^\d+(,\d+)*$"), confirmadas: bool = False):
    from .gsan import escrever_relatorio

    saida = io.BytesIO()
    with conectar() as conn:
        escrever_relatorio(conn, saida, [int(x) for x in ids.split(",")] if ids else None, confirmadas)
    nome = "divergencias_confirmadas.csv" if confirmadas else "divergencias.csv"
    return StreamingResponse(iter([saida.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{nome}"'})


@rotas.post("/gsan/importar")
async def importar_gsan(request: Request, nome: str = Query("gsan.csv", max_length=200)):
    """Recebe o CSV exportado do GSAN e importa em segundo plano. Só leitura: o GSAN não é alterado."""
    dados = await _api().corpo_limitado(request, 200 * 1024 * 1024)
    relativo = f"importacoes/gsan_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    (DATA / relativo).parent.mkdir(parents=True, exist_ok=True)
    (DATA / relativo).write_bytes(dados)
    with conectar() as conn:
        return {"id": T.enfileirar(conn, "importar_gsan", f"Importar extrato do GSAN ({nome[:60]})", arquivo=relativo)}


# ---------------- Modelos de IA ----------------

@rotas.get("/ia")
def ia():
    from .catalogo import CATALOGO, instalado
    from .provedores import ETAPAS, TIPOS, disponibilidade, SQL_MODELO
    from .servidor_local import estado

    with conectar() as conn:
        provedores = conn.execute("""SELECT id, nome, tipo, url, local, chave IS NOT NULL AS tem_chave,
                                            testado_em, teste_ok, teste_mensagem FROM provedor ORDER BY id""").fetchall()
        modelos = conn.execute(SQL_MODELO + " ORDER BY m.id").fetchall()
        etapas = {e["etapa"]: e["modelo_id"] for e in conn.execute("SELECT etapa, modelo_id FROM etapa")}
        downloads = {t["parametros"]["item"]: t for t in conn.execute(
            """SELECT DISTINCT ON (parametros->>'item') id, parametros, estado, atual, total, mensagem
               FROM tarefa WHERE tipo = 'baixar_modelo' ORDER BY parametros->>'item', id DESC""")}
        treinos = {t["nome"]: t["map50"] for t in conn.execute(
            """SELECT nome, (SELECT max((m->>'metrics/mAP50(B)')::float) FROM jsonb_array_elements(metricas) m) AS map50
               FROM treino""")}
    for m in modelos:
        m["situacao"], m["detalhe"] = disponibilidade(m)
        m["etapas"] = [e for e, mid in etapas.items() if mid == m["id"]]
        m["map50"] = treinos.get(m["nome"])
        for k in ("provedor_chave",):
            m.pop(k)  # a chave nunca vai para o navegador
    bonsai = next((p for p in provedores if p["nome"] == "Bonsai local (llama-server)"), None)
    catalogo = [{"chave": k, **{c: v for c, v in item.items() if c in ("nome", "etapa", "descricao", "tamanho")},
                 "instalado": instalado(k), "tarefa": downloads.get(k)} for k, item in CATALOGO.items()]
    return {"tipos": TIPOS, "etapas_nomes": ETAPAS, "etapas": etapas, "provedores": provedores, "modelos": modelos,
            "catalogo": catalogo, "servidor_local": estado(bonsai["url"]) if bonsai else None}


class EscolhaEtapa(BaseModel):
    etapa: Literal["deteccao", "anotacao", "ocr", "classificacao"]
    modelo_id: int | None


@rotas.post("/ia/etapas")
def escolher_etapa(e: EscolhaEtapa):
    from .provedores import TIPOS, modelo_por_id

    with conectar() as conn:
        if e.modelo_id is None:
            if e.etapa != "ocr":
                raise HTTPException(422, "esta etapa precisa de um modelo")
        else:
            try:
                m = modelo_por_id(conn, e.modelo_id)
            except ValueError as erro:
                raise HTTPException(404, str(erro))
            if e.etapa not in TIPOS[m["tipo"]]["etapas"]:
                raise HTTPException(422, f"{m['nome']} não faz esta etapa")
        conn.execute("UPDATE etapa SET modelo_id = %s, alterada_em = now() WHERE etapa = %s", (e.modelo_id, e.etapa))
        registrar(conn, "etapa", 0, e.etapa, {"modelo_id": e.modelo_id})
    return {"ok": True}


class ModeloEntrada(BaseModel):
    nome: str = Field(min_length=2, max_length=80)
    tipo: Literal["grounding_dino", "yolo", "roboflow", "vlm", "paddleocr"]
    provedor_id: int | None = None
    config: dict


def _validar_config(conn, m):
    c = m.config
    texto = lambda k, n=300: isinstance(c.get(k), str) and 0 < len(c[k]) <= n  # noqa: E731
    if m.tipo in ("grounding_dino", "vlm", "roboflow") and not texto("modelo"):
        raise HTTPException(422, "informe o modelo")
    if m.tipo == "yolo":
        pesos = (RAIZ / str(c.get("pesos", ""))).resolve()
        if not texto("pesos") or not pesos.is_relative_to(RAIZ.resolve()) or not pesos.exists():
            raise HTTPException(422, "pesos do YOLO não encontrados dentro do projeto")
    if "limiar" in c and not (isinstance(c["limiar"], (int, float)) and 0.05 <= c["limiar"] <= 0.95):
        raise HTTPException(422, "limiar entre 0,05 e 0,95")
    if "parametros" in c and (not isinstance(c["parametros"], dict) or {"model", "messages"} & set(c["parametros"])):
        raise HTTPException(422, "parâmetros devem ser um objeto JSON, sem 'model' nem 'messages'")
    tipo_provedor = {"vlm": "openai", "roboflow": "roboflow"}.get(m.tipo)
    if tipo_provedor:
        p = conn.execute("SELECT tipo FROM provedor WHERE id = %s", (m.provedor_id,)).fetchone()
        if not p or p["tipo"] != tipo_provedor:
            raise HTTPException(422, "escolha uma conexão compatível com este tipo de modelo")
    elif m.provedor_id is not None:
        raise HTTPException(422, "modelos locais não usam conexão")


@rotas.post("/ia/modelos")
def criar_modelo(m: ModeloEntrada):
    with conectar() as conn:
        _validar_config(conn, m)
        r = conn.execute("""INSERT INTO modelo (nome, tipo, provedor_id, config) VALUES (%s, %s, %s, %s)
                            ON CONFLICT (nome) DO NOTHING RETURNING id""",
                         (m.nome, m.tipo, m.provedor_id, Jsonb(m.config))).fetchone()
        if not r:
            raise HTTPException(409, "já existe um modelo com esse nome")
        registrar(conn, "modelo", r["id"], "criado", m.model_dump())
    return r


@rotas.put("/ia/modelos/{modelo_id}")
def alterar_modelo(modelo_id: int, m: ModeloEntrada):
    with conectar() as conn:
        _validar_config(conn, m)
        if not conn.execute("""UPDATE modelo SET nome = %s, provedor_id = %s, config = %s WHERE id = %s AND tipo = %s
                               RETURNING id""", (m.nome, m.provedor_id, Jsonb(m.config), modelo_id, m.tipo)).fetchone():
            raise HTTPException(404, "modelo não encontrado")
        registrar(conn, "modelo", modelo_id, "alterado", m.model_dump())
    return {"ok": True}


@rotas.delete("/ia/modelos/{modelo_id}")
def excluir_modelo(modelo_id: int):
    with conectar() as conn:
        if conn.execute("SELECT 1 FROM etapa WHERE modelo_id = %s", (modelo_id,)).fetchone():
            raise HTTPException(409, "o modelo está em uso numa etapa; escolha outro antes de excluir")
        conn.execute("DELETE FROM modelo WHERE id = %s", (modelo_id,))
        registrar(conn, "modelo", modelo_id, "excluido")
    return {"ok": True}


class ProvedorEntrada(BaseModel):
    url: str = Field(min_length=8, max_length=300, pattern=r"^https?://")
    chave: str | None = Field(None, max_length=500)  # None = mantém; "" = remove
    local: bool


@rotas.put("/ia/provedores/{provedor_id}")
def alterar_provedor(provedor_id: int, p: ProvedorEntrada):
    with conectar() as conn:
        conn.execute("""UPDATE provedor SET url = %s, local = %s,
                               chave = CASE WHEN %s::text IS NULL THEN chave ELSE nullif(%s, '') END,
                               testado_em = NULL, teste_ok = NULL, teste_mensagem = NULL WHERE id = %s""",
                     (p.url.rstrip("/"), p.local, p.chave, p.chave, provedor_id))
        registrar(conn, "provedor", provedor_id, "alterado", {"url": p.url, "local": p.local,
                                                             "chave": "alterada" if p.chave is not None else "mantida"})
    return {"ok": True}


@rotas.post("/ia/provedores/{provedor_id}/testar")
def testar(provedor_id: int):
    from .provedores import testar_provedor

    with conectar() as conn:
        p = conn.execute("SELECT * FROM provedor WHERE id = %s", (provedor_id,)).fetchone()
        if not p:
            raise HTTPException(404, "conexão não encontrada")
        ok, msg = testar_provedor(p)
        conn.execute("UPDATE provedor SET testado_em = now(), teste_ok = %s, teste_mensagem = %s WHERE id = %s",
                     (ok, msg, provedor_id))
    return {"ok": ok, "mensagem": msg}


@rotas.post("/ia/catalogo/{chave}/instalar")
def instalar(chave: str):
    from .catalogo import CATALOGO

    if chave not in CATALOGO:
        raise HTTPException(404, "item fora do catálogo")
    with conectar() as conn:
        ativa = conn.execute("""SELECT id FROM tarefa WHERE tipo = 'baixar_modelo' AND parametros->>'item' = %s
                                AND estado IN ('fila', 'executando')""", (chave,)).fetchone()
        return ativa or {"id": T.enfileirar(conn, "baixar_modelo", f"Baixar {CATALOGO[chave]['nome']}", item=chave)}


class ServidorLocal(BaseModel):
    modelo: str | None = Field(None, max_length=200)


@rotas.post("/ia/servidor-local")
def servidor_local(s: ServidorLocal):
    """Inicia (com o GGUF escolhido) ou para (modelo vazio) o llama-server local do Bonsai."""
    from . import servidor_local as sl

    with conectar() as conn:
        p = conn.execute("SELECT url FROM provedor WHERE nome = 'Bonsai local (llama-server)'").fetchone()
    try:
        sl.iniciar(p["url"], s.modelo) if s.modelo else sl.parar()
    except ValueError as e:
        raise HTTPException(422, str(e))
    return sl.estado(p["url"])


# ---------------- Datasets ----------------

NOME_DATASET = r"^[a-z0-9][a-z0-9_-]{0,62}$"
CLASSE = r"^[a-z0-9][a-z0-9_]{0,39}$"


@rotas.get("/datasets")
def datasets():
    with conectar() as conn:
        return conn.execute("""
            SELECT d.id, d.nome, d.classes, d.criado_em, count(i.id) AS imagens,
                   count(i.id) FILTER (WHERE i.estado = 'aprovada') AS aprovadas,
                   count(i.id) FILTER (WHERE i.aprovacao = 'automatica') AS aprovadas_auto,
                   count(i.id) FILTER (WHERE i.estado = 'revisar') AS revisar,
                   count(i.id) FILTER (WHERE i.estado = 'vazia') AS vazias,
                   count(i.id) FILTER (WHERE i.conjunto = 'treino') AS treino,
                   count(i.id) FILTER (WHERE i.conjunto = 'validacao') AS validacao,
                   count(i.id) FILTER (WHERE i.conjunto = 'teste') AS teste,
                   (SELECT count(*) FROM anotacao a JOIN dataset_imagem x ON x.id = a.imagem_id
                    WHERE x.dataset_id = d.id AND a.origem <> 'sugestao') AS caixas
            FROM dataset d LEFT JOIN dataset_imagem i ON i.dataset_id = d.id GROUP BY d.id ORDER BY d.id""").fetchall()


class NovoDataset(BaseModel):
    nome: str = Field(pattern=NOME_DATASET)
    classes: list[str] = Field(min_length=1, max_length=40)
    origem: Literal["vazio", "fotos", "revisadas"] = "vazio"


def _classes_validas(classes):
    if len(set(classes)) != len(classes) or not all(re.match(CLASSE, c) for c in classes):
        raise HTTPException(422, "classes: letras minúsculas, números e _, sem repetir")


@rotas.post("/datasets")
def criar_dataset(d: NovoDataset):
    from .datasets import criar

    _classes_validas(d.classes)
    with conectar() as conn:
        if conn.execute("SELECT 1 FROM dataset WHERE nome = %s", (d.nome,)).fetchone():
            raise HTTPException(409, "já existe um dataset com esse nome")
        return {"id": criar(conn, d.nome, d.classes, d.origem)}


@rotas.get("/datasets/{dataset_id}")
def dataset(dataset_id: int):
    with conectar() as conn:
        d = conn.execute("SELECT * FROM dataset WHERE id = %s", (dataset_id,)).fetchone()
        if not d:
            raise HTTPException(404, "dataset não encontrado")
        d["imagens"] = conn.execute("""
            SELECT i.id, i.foto_id, i.nome, i.largura, i.altura, i.conjunto, i.estado,
                   count(a.id) FILTER (WHERE a.origem <> 'sugestao') AS caixas,
                   count(a.id) FILTER (WHERE a.origem = 'sugestao') AS sugestoes,
                   coalesce(array_agg(DISTINCT a.classe) FILTER (WHERE a.id IS NOT NULL), '{}') AS classes
            FROM dataset_imagem i LEFT JOIN anotacao a ON a.imagem_id = i.id
            WHERE i.dataset_id = %s GROUP BY i.id ORDER BY i.id""", (dataset_id,)).fetchall()
        d["uso_classes"] = {r["classe"]: r["n"] for r in conn.execute(
            """SELECT a.classe, count(*) AS n FROM anotacao a JOIN dataset_imagem i ON i.id = a.imagem_id
               WHERE i.dataset_id = %s AND a.origem <> 'sugestao' GROUP BY a.classe""", (dataset_id,))}
    return d


class AlterarDataset(BaseModel):
    classes: list[str] | None = Field(None, min_length=1, max_length=40)
    textos_busca: dict[str, str] | None = None


@rotas.patch("/datasets/{dataset_id}")
def alterar_dataset(dataset_id: int, a: AlterarDataset):
    with conectar() as conn:
        d = conn.execute("SELECT classes FROM dataset WHERE id = %s FOR UPDATE", (dataset_id,)).fetchone()
        if not d:
            raise HTTPException(404, "dataset não encontrado")
        if a.classes is not None:
            _classes_validas(a.classes)
            removidas = set(d["classes"]) - set(a.classes)
            if removidas and conn.execute("""SELECT 1 FROM anotacao x JOIN dataset_imagem i ON i.id = x.imagem_id
                                             WHERE i.dataset_id = %s AND x.classe = ANY (%s) LIMIT 1""",
                                          (dataset_id, list(removidas))).fetchone():
                raise HTTPException(409, "só é possível excluir classes sem caixas")
            conn.execute("UPDATE dataset SET classes = %s WHERE id = %s", (a.classes, dataset_id))
        if a.textos_busca is not None:
            textos = {k: v.strip()[:120] for k, v in a.textos_busca.items() if re.match(CLASSE, k)}
            conn.execute("UPDATE dataset SET textos_busca = %s WHERE id = %s", (Jsonb(textos), dataset_id))
        registrar(conn, "dataset", dataset_id, "alterado", a.model_dump(exclude_none=True))
    return {"ok": True}


@rotas.post("/datasets/{dataset_id}/imagens")
async def enviar_imagem(dataset_id: int, request: Request, nome: str = Query(max_length=200)):
    from .datasets import MAX_UPLOAD, adicionar_upload

    dados = await _api().corpo_limitado(request, MAX_UPLOAD)
    with conectar() as conn:
        if not conn.execute("SELECT 1 FROM dataset WHERE id = %s", (dataset_id,)).fetchone():
            raise HTTPException(404, "dataset não encontrado")
        try:
            return {"id": adicionar_upload(conn, dataset_id, nome, dados)}
        except ValueError as e:
            raise HTTPException(422, str(e))


class FotosDataset(BaseModel):
    foto_ids: list[int] = Field(min_length=1, max_length=5000)


@rotas.post("/datasets/{dataset_id}/fotos")
def adicionar_fotos(dataset_id: int, f: FotosDataset):
    from .datasets import adicionar_fotos as adicionar

    with conectar() as conn:
        return {"adicionadas": len(adicionar(conn, dataset_id, f.foto_ids))}


@rotas.get("/dataset-imagens/{imagem_id}")
def imagem_dataset(imagem_id: int):
    with conectar() as conn:
        i = conn.execute("SELECT * FROM dataset_imagem WHERE id = %s", (imagem_id,)).fetchone()
        if not i:
            raise HTTPException(404, "imagem não encontrada")
        i["anotacoes"] = conn.execute("""SELECT id, classe, bbox, origem, score, modelo FROM anotacao
                                         WHERE imagem_id = %s ORDER BY id""", (imagem_id,)).fetchall()
    return i


@rotas.get("/dataset-imagens/{imagem_id}/imagem")
def arquivo_imagem_dataset(imagem_id: int, w: int = 1600):
    with conectar() as conn:
        i = conn.execute("SELECT arquivo FROM dataset_imagem WHERE id = %s", (imagem_id,)).fetchone()
    if not i:
        raise HTTPException(404, "imagem não encontrada")
    return _api().imagem_redimensionada(i["arquivo"], w, f"ds{imagem_id}")


class AnotacaoEntrada(BaseModel):
    classe: str = Field(max_length=40)
    bbox: list[int] = Field(min_length=4, max_length=4)
    origem: Literal["manual", "sugestao", "aceita"]
    score: float | None = None
    modelo: str | None = Field(None, max_length=80)


class Anotacoes(BaseModel):
    anotacoes: list[AnotacaoEntrada] = Field(max_length=500)
    aprovar: bool = False


@rotas.put("/dataset-imagens/{imagem_id}/anotacoes")
def salvar_anotacoes(imagem_id: int, a: Anotacoes):
    from .datasets import salvar_anotacoes as salvar

    with conectar() as conn:
        try:
            return {"estado": salvar(conn, imagem_id, [x.model_dump() for x in a.anotacoes], a.aprovar)}
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))


class Conjunto(BaseModel):
    conjunto: Literal["treino", "validacao", "teste"]


@rotas.patch("/dataset-imagens/{imagem_id}")
def mudar_conjunto(imagem_id: int, c: Conjunto):
    with conectar() as conn:
        conn.execute("UPDATE dataset_imagem SET conjunto = %s WHERE id = %s", (c.conjunto, imagem_id))
        registrar(conn, "dataset_imagem", imagem_id, "conjunto", c.model_dump())
    return {"ok": True}


@rotas.delete("/dataset-imagens/{imagem_id}")
def remover_imagem(imagem_id: int):
    """Tira a imagem do dataset. A foto do GeoFAT continua cadastrada; só o arquivo enviado ao dataset é apagado."""
    from .config import FOTOS_DIR

    with conectar() as conn:
        i = conn.execute("DELETE FROM dataset_imagem WHERE id = %s RETURNING arquivo, foto_id, dataset_id",
                         (imagem_id,)).fetchone()
        if i:
            registrar(conn, "dataset", i["dataset_id"], "imagem_removida", {"imagem_id": imagem_id})
    if i and i["foto_id"] is None and i["arquivo"].startswith("datasets/"):
        (FOTOS_DIR / i["arquivo"]).unlink(missing_ok=True)
    return {"ok": True}


class Sugestao(BaseModel):
    imagem_ids: list[int] = Field(min_length=1, max_length=5000)
    modelo_id: int
    limiar: float | None = Field(None, ge=0.05, le=0.95)


@rotas.post("/datasets/{dataset_id}/sugerir")
def sugerir(dataset_id: int, s: Sugestao):
    with conectar() as conn:
        n = len(s.imagem_ids)
        return {"id": T.enfileirar(conn, "sugerir_anotacoes", f"Sugerir anotações · {n} imagem(ns)",
                                   dataset_id=dataset_id, imagem_ids=s.imagem_ids, modelo_id=s.modelo_id, limiar=s.limiar)}


@rotas.post("/datasets/{dataset_id}/exportar")
def exportar(dataset_id: int):
    with conectar() as conn:
        d = conn.execute("SELECT nome FROM dataset WHERE id = %s", (dataset_id,)).fetchone()
        if not d:
            raise HTTPException(404, "dataset não encontrado")
        return {"id": T.enfileirar(conn, "exportar_dataset", f"Exportar {d['nome']} (YOLO)", dataset_id=dataset_id)}


# ---------------- Mapillary e pré-rotulagem automática ----------------

def _token_mapillary(conn):
    p = conn.execute("SELECT chave FROM provedor WHERE tipo = 'mapillary'").fetchone()
    if not p or not p["chave"]:
        raise HTTPException(409, "configure o Client Token do Mapillary primeiro")
    return p["chave"]


@rotas.get("/prerotulagem")
def prerotulagem():
    from . import mapillary as mp
    from .prerotulagem import CRITERIOS_PADRAO, MAX_TENTATIVAS, resumo
    from .provedores import SQL_MODELO, TIPOS, disponibilidade

    with conectar() as conn:
        conexao = conn.execute("""SELECT id, url, chave IS NOT NULL AS tem_chave, testado_em, teste_ok, teste_mensagem
                                  FROM provedor WHERE tipo = 'mapillary'""").fetchone()
        modelos = conn.execute(SQL_MODELO + " ORDER BY m.id").fetchall()
        trabalhos = conn.execute("""
            SELECT p.id, p.dataset_id, d.nome AS dataset, p.config, cardinality(p.imagem_ids) AS imagens, p.criada_em,
                   t.id AS tarefa_id, t.estado, t.atual, t.total, t.unidade, t.mensagem, t.iniciada_em, t.terminada_em
            FROM prerotulagem p JOIN dataset d ON d.id = p.dataset_id
            LEFT JOIN LATERAL (SELECT * FROM tarefa x WHERE x.tipo = 'prerotular'
                               AND (x.parametros->>'prerotulagem_id')::int = p.id ORDER BY x.id DESC LIMIT 1) t ON true
            ORDER BY p.id DESC LIMIT 20""").fetchall()
        for t in trabalhos:
            t["resumo"] = resumo(conn, t["id"])
        importacoes = conn.execute(f"""SELECT {COLUNAS_TAREFA} FROM tarefa WHERE tipo = 'importar_mapillary'
                                       ORDER BY id DESC LIMIT 5""").fetchall()
        fotos = conn.execute("SELECT count(*) AS n FROM foto_procedencia WHERE fonte = 'mapillary'").fetchone()["n"]
    escolha = lambda m: {"id": m["id"], "nome": m["nome"], "tipo": m["tipo"],  # noqa: E731
                         "local": m["tipo"] != "vlm" or bool(m["provedor_local"]),
                         "limiar": m["config"].get("limiar"), "situacao": disponibilidade(m)}
    return {"mapillary": conexao, "licenca": {"nome": mp.LICENCA, "termos_url": mp.TERMOS_URL, "condicoes": mp.CONDICOES},
            "area_max": mp.AREA_MAX, "fotos_mapillary": fotos, "importacoes": importacoes, "trabalhos": trabalhos,
            "detectores": [escolha(m) for m in modelos if "anotacao" in TIPOS[m["tipo"]]["etapas"]],
            "verificadores": [escolha(m) for m in modelos if m["tipo"] == "vlm"],
            "criterios_padrao": CRITERIOS_PADRAO, "max_tentativas": MAX_TENTATIVAS}


@rotas.get("/mapillary/buscar")
def buscar_mapillary(bbox: str = Query(pattern=r"^-?[\d.]+,-?[\d.]+,-?[\d.]+,-?[\d.]+$"), incluir_pano: bool = False):
    """Prévia real das imagens disponíveis na área (nada é baixado). O token não sai do servidor."""
    from .mapillary import buscar

    caixa = [float(v) for v in bbox.split(",")]
    with conectar() as conn:
        token = _token_mapillary(conn)
        ja = {r["id_externo"] for r in conn.execute("SELECT id_externo FROM foto_procedencia WHERE fonte = 'mapillary'")}
    try:
        achadas = buscar(token, caixa, 2000, incluir_pano)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"total": len(achadas), "ja_baixadas": sum(n["id"] in ja for n in achadas),
            "sequencias": len({n["sequencia"] for n in achadas}),
            "imagens": [{k: n[k] for k in ("id", "lat", "lon", "angulo", "capturada_em", "autor", "sequencia")} for n in achadas]}


class ImportarMapillary(BaseModel):
    bbox: list[float] = Field(min_length=4, max_length=4)
    limite: int = Field(200, ge=1, le=2000)
    dataset_id: int | None = None
    novo_dataset: str | None = Field(None, pattern=NOME_DATASET)
    incluir_pano: bool = False
    licenca_verificada: bool


@rotas.post("/mapillary/importar")
def importar_mapillary(i: ImportarMapillary):
    from . import mapillary as mp
    from .datasets import criar

    if not i.licenca_verificada:
        raise HTTPException(422, "confirme que verificou as condições de licença antes de baixar as imagens")
    try:
        mp.validar_bbox(i.bbox)
    except ValueError as e:
        raise HTTPException(422, str(e))
    with conectar() as conn:
        _token_mapillary(conn)
        dataset_id = i.dataset_id
        if i.novo_dataset:
            if conn.execute("SELECT 1 FROM dataset WHERE nome = %s", (i.novo_dataset,)).fetchone():
                raise HTTPException(409, "já existe um dataset com esse nome")
            dataset_id = criar(conn, i.novo_dataset, ["fachada"])
        elif dataset_id and not conn.execute("SELECT 1 FROM dataset WHERE id = %s", (dataset_id,)).fetchone():
            raise HTTPException(404, "dataset não encontrado")
        registrar(conn, "mapillary", dataset_id or 0, "licenca_verificada",
                  {"licenca": mp.LICENCA, "termos_url": mp.TERMOS_URL, "condicoes": mp.CONDICOES, "bbox": i.bbox})
        tarefa_id = T.enfileirar(conn, "importar_mapillary", f"Importar do Mapillary · até {i.limite} imagem(ns)",
                                 bbox=i.bbox, limite=i.limite, dataset_id=dataset_id, incluir_pano=i.incluir_pano)
    return {"id": tarefa_id, "dataset_id": dataset_id}


class Criterios(BaseModel):
    confianca_verificador: float = Field(ge=0, le=1)
    score_detector: float = Field(ge=0, le=1)
    omitidas_max: int = Field(ge=0, le=20)


class NovaPrerotulagem(BaseModel):
    dataset_id: int
    detector_id: int
    verificador_id: int
    max_tentativas: int = Field(3, ge=1, le=5)
    limiar_inicial: float | None = Field(None, ge=0.05, le=0.95)
    criterios: Criterios
    revisao_humana: bool = False
    amostra_pct: float = Field(10, ge=5, le=100)


@rotas.post("/prerotulagem")
def criar_prerotulagem(n: NovaPrerotulagem):
    from .prerotulagem import sortear_amostra
    from .provedores import TIPOS, modelo_por_id

    with conectar() as conn:
        ds = conn.execute("SELECT nome, classes FROM dataset WHERE id = %s", (n.dataset_id,)).fetchone()
        if not ds:
            raise HTTPException(404, "dataset não encontrado")
        if "fachada" not in ds["classes"]:
            raise HTTPException(422, "a pré-rotulagem anota a classe 'fachada', que este dataset não tem")
        try:
            det, ver = modelo_por_id(conn, n.detector_id), modelo_por_id(conn, n.verificador_id)
        except ValueError as e:
            raise HTTPException(404, str(e))
        if "anotacao" not in TIPOS[det["tipo"]]["etapas"] or ver["tipo"] != "vlm":
            raise HTTPException(422, "escolha um detector e um modelo de visão e linguagem como verificador")
        # Imagens sem caixas e não reservadas por outra pré-rotulagem ainda não concluída (essa se retoma)
        ids = [r["id"] for r in conn.execute("""
            SELECT i.id FROM dataset_imagem i WHERE i.dataset_id = %s AND i.estado = 'vazia'
              AND NOT EXISTS (SELECT 1 FROM prerotulagem p WHERE i.id = ANY (p.imagem_ids) AND NOT EXISTS
                              (SELECT 1 FROM prerotulo r WHERE r.prerotulagem_id = p.id AND r.imagem_id = i.id))
            ORDER BY i.id""", (n.dataset_id,))]
        if not ids:
            raise HTTPException(409, "nenhuma imagem sem anotação para pré-rotular (as reservadas por uma pré-rotulagem "
                                     "interrompida continuam com 'Retomar')")
        amostra = sortear_amostra(ids, n.amostra_pct)
        conn.execute("UPDATE dataset_imagem SET amostra_avaliacao = true WHERE id = ANY (%s)", (amostra,))
        config = n.model_dump(exclude={"dataset_id"})
        p = conn.execute("INSERT INTO prerotulagem (dataset_id, config, imagem_ids) VALUES (%s, %s, %s) RETURNING id",
                         (n.dataset_id, Jsonb(config), ids)).fetchone()["id"]
        tarefa_id = T.enfileirar(conn, "prerotular", f"Pré-rotular {ds['nome']} · {len(ids)} imagem(ns)", prerotulagem_id=p)
        conn.execute("UPDATE prerotulagem SET tarefa_id = %s WHERE id = %s", (tarefa_id, p))
        registrar(conn, "prerotulagem", p, "criada", {**config, "imagens": len(ids), "amostra": amostra})
    return {"id": p, "tarefa_id": tarefa_id, "imagens": len(ids), "amostra": len(amostra)}


# ---------------- Treinos ----------------

@rotas.get("/treinos")
def treinos():
    from .treino import bases_disponiveis

    with conectar() as conn:
        lista = conn.execute("""
            SELECT t.id, t.nome, t.dataset_id, d.nome AS dataset, t.parametros, t.classes, t.imagens, t.metricas,
                   t.pesos, t.criado_em, x.id AS tarefa_id, x.estado, x.atual, x.total, x.mensagem, x.iniciada_em,
                   x.terminada_em, (SELECT id FROM modelo WHERE nome = t.nome) AS modelo_id
            FROM treino t LEFT JOIN dataset d ON d.id = t.dataset_id LEFT JOIN tarefa x ON x.id = t.tarefa_id
            ORDER BY t.id DESC""").fetchall()
        em_uso = conn.execute("""SELECT m.nome, m.tipo FROM etapa e JOIN modelo m ON m.id = e.modelo_id
                                 WHERE e.etapa = 'deteccao'""").fetchone()
        return {"treinos": lista, "bases": bases_disponiveis(conn), "detector_em_uso": em_uso}


class NovoTreino(BaseModel):
    dataset_id: int
    base: str = Field(max_length=300)
    epocas: int = Field(ge=1, le=300)
    imgsz: int = Field(640, ge=320, le=1280, multiple_of=32)
    lote: int = Field(8, ge=1, le=64)


@rotas.post("/treinos")
def treinar(n: NovoTreino):
    from .treino import criar

    with conectar() as conn:
        try:
            t = criar(conn, n.dataset_id, n.base, n.epocas, n.imgsz, n.lote)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))
        tarefa_id = T.enfileirar(conn, "treinar", f"Treinar {t['nome']}", treino_id=t["id"])
        conn.execute("UPDATE treino SET tarefa_id = %s WHERE id = %s", (tarefa_id, t["id"]))
        registrar(conn, "treino", t["id"], "criado", n.model_dump())
    return {"id": t["id"], "tarefa_id": tarefa_id}


@rotas.post("/treinos/{treino_id}/usar")
def usar_treino(treino_id: int):
    """Registra a versão treinada como detector YOLO, que passa a aparecer nas etapas da página Modelos."""
    with conectar() as conn:
        t = conn.execute("SELECT nome, pesos, classes FROM treino WHERE id = %s", (treino_id,)).fetchone()
        if not t or not t["pesos"]:
            raise HTTPException(409, "o treino ainda não gerou pesos")
        r = conn.execute("""INSERT INTO modelo (nome, tipo, config) VALUES (%s, 'yolo', %s)
                            ON CONFLICT (nome) DO UPDATE SET config = excluded.config RETURNING id""",
                         (t["nome"], Jsonb({"pesos": t["pesos"], "limiar": 0.35, "classes": t["classes"]}))).fetchone()
        registrar(conn, "treino", treino_id, "registrado_como_modelo", {"modelo_id": r["id"]})
    return r


# ---------------- Ambiente de processamento (local ou GPU remota) ----------------

class Processamento(BaseModel):
    ambiente: Literal["local", "runpod"]
    custo_hora_usd: float | None = Field(None, ge=0, le=100)
    orcamento_usd: float | None = Field(None, ge=0, le=100000)


@rotas.get("/processamento")
def ver_processamento():
    from .provedores import OCORRENCIAS_REMOTAS, processamento

    with conectar() as conn:
        p = processamento(conn)
        remoto = conn.execute("""SELECT id, nome, url, chave IS NOT NULL AS tem_chave, testado_em, teste_ok, teste_mensagem
                                 FROM provedor WHERE tipo = 'gpu_remota' ORDER BY id LIMIT 1""").fetchone()
    p["remoto"] = remoto  # sem a chave
    p["ocorrencias"] = list(OCORRENCIAS_REMOTAS)[-20:]
    return p


@rotas.put("/processamento")
def alterar_processamento(p: Processamento):
    with conectar() as conn:
        if p.ambiente == "runpod" and not conn.execute(
                "SELECT 1 FROM provedor WHERE tipo = 'gpu_remota' AND chave IS NOT NULL").fetchone():
            raise HTTPException(422, "informe o endereço e o token do serviço GPU antes de escolher o Runpod")
        conn.execute("UPDATE configuracao SET valor = %s, alterada_em = now() WHERE chave = 'processamento'",
                     (Jsonb(p.model_dump()),))
        registrar(conn, "configuracao", 0, "processamento", p.model_dump())
    return {"ok": True}


@rotas.get("/processamento/status")
def status_remoto():
    """Status ao vivo do serviço GPU (GPU, VRAM, modelos carregados, tarefas, erros). O token fica no servidor."""
    import urllib.error

    from .provedores import chamar_remoto, processamento

    with conectar() as conn:
        r = processamento(conn)["remoto"]
    if not r or not r["chave"]:
        raise HTTPException(409, "serviço GPU sem endereço ou token (Modelos > Conexões > Runpod GPU)")
    try:
        return chamar_remoto({"url": r["url"].rstrip("/"), "chave": r["chave"]}, "/status", timeout=10)
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"serviço GPU respondeu HTTP {e.code}" + (": token recusado" if e.code in (401, 403) else ""))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HTTPException(503, f"serviço GPU sem resposta ({getattr(e, 'reason', e)}); o pod está ligado?")


# ---------------- Análise direcionada ----------------

@rotas.get("/direcionada")
def direcionada():
    from .direcionada import FOTOS_POR_ALVO, MAX_ALVOS, MAX_FOTOS_POR_ALVO, estimar, medicoes, resumo
    from .localizar import ALCANCE_M
    from .provedores import processamento

    with conectar() as conn:
        alvos = conn.execute("""SELECT a.id, a.matricula, a.localidade, a.origem, a.linha, ST_Y(a.geom) AS lat, ST_X(a.geom) AS lon,
                                       (SELECT x.resultado->>'situacao' FROM analise_alvo x WHERE x.alvo_id = a.id
                                        AND x.resultado ? 'situacao' ORDER BY x.analise_id DESC LIMIT 1) AS situacao
                                FROM alvo a ORDER BY a.id DESC LIMIT 200""").fetchall()
        analises = conn.execute("""
            SELECT a.id, a.ambiente, a.config, a.alvo_ids, round(a.gasto_s) AS gasto_s, a.criada_em,
                   t.id AS tarefa_id, t.estado, t.atual, t.total, t.unidade, t.mensagem, t.iniciada_em, t.terminada_em
            FROM analise a
            LEFT JOIN LATERAL (SELECT * FROM tarefa x WHERE x.tipo = 'analisar_alvos'
                               AND (x.parametros->>'analise_id')::int = a.id ORDER BY x.id DESC LIMIT 1) t ON true
            ORDER BY a.id DESC LIMIT 20""").fetchall()
        med = medicoes(conn)
        for a in analises:
            a["resumo"], a["estimativa_s"] = resumo(conn, a["id"]), estimar(conn, a["id"], med)
            a["alvos"] = conn.execute("""
                SELECT x.alvo_id, v.matricula, x.estado, x.edificacao_id, x.edificacao_evidencias, x.buscadas, x.resultado, x.erro,
                       (SELECT coalesce(json_agg(json_build_object('foto_id', f.foto_id, 'etapa', f.etapa, 'associacao', f.associacao,
                                'imovel_id', f.imovel_id, 'situacao', f.avaliacao->>'situacao', 'distancia_m', f.avaliacao->'distancia_m',
                                'caixas', jsonb_array_length(coalesce(f.deteccao->'caixas', '[]')),
                                'verificacao', f.verificacao->>'problema', 'categoria', f.classificacao->>'categoria',
                                'confianca', f.classificacao->'confianca', 'tempos', f.tempos, 'erro', f.erro) ORDER BY f.foto_id), '[]')
                        FROM analise_foto f WHERE f.analise_id = x.analise_id AND f.alvo_id = x.alvo_id) AS fotos
                FROM analise_alvo x JOIN alvo v ON v.id = x.alvo_id WHERE x.analise_id = %s ORDER BY x.alvo_id""",
                                      (a["id"],)).fetchall()
        importacoes = conn.execute(f"""SELECT {COLUNAS_TAREFA} FROM tarefa WHERE tipo = 'importar_alvos'
                                       ORDER BY id DESC LIMIT 5""").fetchall()
        proc = processamento(conn)
    return {"alvos": alvos, "analises": analises, "importacoes": importacoes, "medicoes": med,
            "ambiente": proc["ambiente"], "custo_hora_usd": proc.get("custo_hora_usd"),
            "limites": {"max_alvos": MAX_ALVOS, "fotos_por_alvo": FOTOS_POR_ALVO, "max_fotos_por_alvo": MAX_FOTOS_POR_ALVO,
                        "raio_m": ALCANCE_M}}


@rotas.post("/direcionada/planilha")
async def enviar_planilha(request: Request, nome: str = Query(max_length=200), quantidade: int = Query(3, ge=1, le=50),
                          semente: int = Query(1, ge=0, le=10**9), localidade: str | None = Query(None, max_length=120),
                          matriculas: str | None = Query(None, max_length=5000), exigir_foto: bool = True):
    """Recebe a planilha (.xlsx ou .csv) e cadastra só `quantidade` alvos, em segundo plano. A cópia enviada é
    apagada depois da leitura: dela ficam só matrícula, coordenada e localidade dos alvos escolhidos."""
    extensao = ".xlsx" if nome.lower().endswith(".xlsx") else ".csv" if nome.lower().endswith(".csv") else None
    if not extensao:
        raise HTTPException(422, "envie uma planilha .xlsx ou .csv")
    dados = await _api().corpo_limitado(request, 100 * 1024 * 1024)
    relativo = f"importacoes/alvos_{time.strftime('%Y%m%d_%H%M%S')}{extensao}"
    (DATA / relativo).parent.mkdir(parents=True, exist_ok=True)
    (DATA / relativo).write_bytes(dados)
    lista = [m for m in re.split(r"[\s,;]+", matriculas or "") if m]
    with conectar() as conn:
        return {"id": T.enfileirar(conn, "importar_alvos", f"Alvos da planilha {nome[:60]} · até {quantidade}",
                                   arquivo=relativo, origem=nome[:200], quantidade=quantidade, semente=semente,
                                   localidade=localidade or None, matriculas=lista or None, exigir_foto=exigir_foto,
                                   apagar=True)}


class NovaAnalise(BaseModel):
    alvo_ids: list[int] = Field(min_length=1, max_length=50)
    fotos_por_alvo: int = Field(3, ge=1, le=10)
    raio_m: int = Field(60, ge=15, le=100)
    verificar: bool = True
    tempo_max_min: int | None = Field(None, ge=1, le=24 * 60)
    reprocessar: bool = False
    licenca_verificada: bool


@rotas.post("/direcionada/analises")
def criar_analise(n: NovaAnalise):
    from . import mapillary as mp
    from .direcionada import criar_analise as criar

    if not n.licenca_verificada:
        raise HTTPException(422, "confirme que verificou as condições de licença do Mapillary antes de baixar as imagens")
    with conectar() as conn:
        _token_mapillary(conn)
        if conn.execute("SELECT count(*) AS n FROM alvo WHERE id = ANY (%s)", (n.alvo_ids,)).fetchone()["n"] != len(set(n.alvo_ids)):
            raise HTTPException(404, "alvo não encontrado")
        cfg = n.model_dump(exclude={"alvo_ids", "reprocessar", "licenca_verificada"})
        try:
            analise_id, pulados = criar(conn, n.alvo_ids, cfg, n.reprocessar)
        except ValueError as e:
            raise HTTPException(422, str(e))
        if not analise_id:
            raise HTTPException(409, "todos os alvos já foram analisados neste ambiente; marque 'reprocessar' para comparar")
        registrar(conn, "mapillary", 0, "licenca_verificada",
                  {"licenca": mp.LICENCA, "termos_url": mp.TERMOS_URL, "condicoes": mp.CONDICOES, "analise_id": analise_id})
        tarefa_id = T.enfileirar(conn, "analisar_alvos", f"Análise direcionada #{analise_id} · {len(n.alvo_ids) - len(pulados)} alvo(s)",
                                 analise_id=analise_id)
    return {"id": analise_id, "tarefa_id": tarefa_id, "pulados": pulados}


@rotas.get("/direcionada/comparar")
def comparar_analises(a: int, b: int):
    from .direcionada import comparar

    with conectar() as conn:
        return comparar(conn, a, b)


@rotas.get("/alvos")
def alvos_geojson():
    """Alvos para o mapa, com a situação e as observações da análise mais recente de cada um."""
    with conectar() as conn:
        return conn.execute("""
            SELECT json_build_object('type', 'FeatureCollection', 'features', coalesce(json_agg(ST_AsGeoJSON(t.*)::json), '[]'))
            FROM (SELECT a.id, a.matricula, a.localidade, x.analise_id, x.estado, x.resultado->>'situacao' AS situacao,
                         (SELECT coalesce(json_agg(json_build_object('imovel_id', f.imovel_id, 'associacao', f.associacao,
                                  'categoria', f.classificacao->>'categoria')), '[]')
                          FROM analise_foto f WHERE f.analise_id = x.analise_id AND f.alvo_id = a.id AND f.imovel_id IS NOT NULL) AS observacoes,
                         a.geom
                  FROM alvo a LEFT JOIN LATERAL (SELECT * FROM analise_alvo y WHERE y.alvo_id = a.id  -- a última concluída
                                                 ORDER BY coalesce(y.resultado ? 'situacao', false) DESC, y.analise_id DESC LIMIT 1) x ON true) t""").fetchone()["json_build_object"]
