"""API e interface web: uvicorn geofat.api:app"""
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from .config import FOTOS_DIR, RAIZ
from .db import conectar, migrar, registrar
from .fotos import abrir
from .localizar import localizar, posicionar_manual

CACHE = RAIZ / "data" / "cache"


@asynccontextmanager
async def ciclo(app):
    from . import tarefas

    with conectar() as conn:
        migrar(conn)
    parar = tarefas.iniciar_executores()
    yield
    parar.set()


app = FastAPI(title="GeoFAT", lifespan=ciclo)
Categoria = Literal["residencial", "comercial", "industrial", "institucional", "misto", "terreno_vazio", "inconclusivo"]


def imagem_redimensionada(relativo, largura, chave):
    """JPEG com a rotação do EXIF aplicada e no máximo `largura` px, guardado em data/cache."""
    largura = max(64, min(largura, 2400))
    destino = CACHE / f"{chave}_{largura}.jpg"
    origem = FOTOS_DIR / relativo
    if not origem.exists():
        raise HTTPException(404, "arquivo da imagem não encontrado")
    if not destino.exists() or destino.stat().st_mtime < origem.stat().st_mtime:
        img = abrir(origem)
        img.thumbnail((largura, largura * 4))
        destino.parent.mkdir(parents=True, exist_ok=True)
        img.save(destino, "JPEG", quality=85)
    return FileResponse(destino, media_type="image/jpeg", headers={"Cache-Control": "max-age=300"})


@app.get("/api/resumo")
def resumo():
    """Indicadores reais do banco para a Visão geral e os contadores do menu."""
    with conectar() as conn:
        r = conn.execute("""
            SELECT (SELECT count(*) FROM foto) AS fotos,
                   (SELECT count(*) FROM foto WHERE detectada) AS fotos_detectadas,
                   (SELECT count(*) FROM foto WHERE direcao IS NOT NULL) AS fotos_com_direcao,
                   count(*) AS imoveis,
                   count(*) FILTER (WHERE fachada_valida IS DISTINCT FROM false) AS validos,
                   count(*) FILTER (WHERE categoria_ia IS NOT NULL AND fachada_valida IS DISTINCT FROM false) AS classificados,
                   count(*) FILTER (WHERE revisado_em IS NOT NULL AND fachada_valida IS DISTINCT FROM false) AS revisados,
                   count(*) FILTER (WHERE revisado_em IS NULL AND fachada_valida IS DISTINCT FROM false) AS pendentes_revisao,
                   count(*) FILTER (WHERE localizacao_confianca <> 'baixa' AND fachada_valida IS DISTINCT FROM false) AS localizados,
                   count(*) FILTER (WHERE associacao = 'confirmada' AND fachada_valida IS DISTINCT FROM false) AS associados,
                   (SELECT count(*) FROM gsan_ligacao) AS gsan_ligacoes,
                   (SELECT max(em) FROM historico WHERE entidade = 'gsan' AND acao = 'importacao') AS gsan_importado_em,
                   (SELECT count(*) FROM tarefa WHERE estado = 'executando') AS tarefas_executando,
                   (SELECT count(*) FROM tarefa WHERE estado = 'fila') AS tarefas_fila,
                   (SELECT count(*) FROM decisao_comercial WHERE decisao = 'confirmada') AS decisoes_confirmadas
            FROM imovel_analise""").fetchone()
        r["situacoes"] = {x["situacao"]: x["n"] for x in conn.execute(
            "SELECT situacao, count(*) AS n FROM imovel_analise GROUP BY situacao")}
        r["categorias"] = {x["categoria_final"] or "sem_categoria": x["n"] for x in conn.execute(
            "SELECT categoria_final, count(*) AS n FROM imovel_analise WHERE fachada_valida IS DISTINCT FROM false GROUP BY 1")}
    return r


@app.get("/api/imoveis")
def imoveis():
    with conectar() as conn:
        return conn.execute("""
            SELECT json_build_object('type', 'FeatureCollection',
                                     'features', coalesce(json_agg(ST_AsGeoJSON(a.*)::json), '[]'))
            FROM (SELECT a.id, a.foto_id, a.categoria_final, a.situacao, a.localizacao_confianca, a.localizacao_fonte,
                         a.edificacao_id, i.grupo_id, a.revisado_em IS NOT NULL AS revisado, a.geom
                  FROM imovel_analise a JOIN imovel i ON i.id = a.id) a""").fetchone()["json_build_object"]


@app.get("/api/imoveis/{imovel_id}")
def imovel(imovel_id: int):
    with conectar() as conn:
        d = conn.execute("""
            SELECT a.id, a.foto_id, a.bbox, a.bbox_detectada, a.detector, a.score_deteccao,
                   a.categoria_ia, a.unidades_ia, a.confianca_ia, a.evidencias_ia, a.limitacoes_ia, a.modelo_ia,
                   a.categoria_revisada, a.unidades_revisadas, a.fachada_valida, a.observacao, a.revisado_em,
                   a.categoria_final, a.unidades_final, a.edificacao_id, a.localizacao_fonte, a.localizacao_confianca,
                   a.situacao, ST_Y(a.geom) AS lat, ST_X(a.geom) AS lon, ST_Y(a.geom_camera) AS lat_camera,
                   ST_X(a.geom_camera) AS lon_camera, i.texto_ocr, (SELECT count(*) FROM gsan_ligacao) AS gsan_ligacoes,
                   f.arquivo, f.fonte, f.capturada_em, f.largura, f.altura, f.direcao, f.fov_horizontal, f.credito,
                   (SELECT fonte FROM edificacao WHERE id = a.edificacao_id) AS edificacao_fonte,
                   (SELECT row_to_json(d) FROM decisao_comercial d WHERE d.imovel_id = a.id) AS decisao,
                   (SELECT array_agg(id ORDER BY id) FROM imovel WHERE foto_id = a.foto_id) AS fachadas_da_foto,
                   (SELECT json_build_object('type', 'FeatureCollection', 'features',
                                             coalesce(json_agg(ST_AsGeoJSON(e.*)::json), '[]'))
                    FROM (SELECT id, fonte, confianca, geom FROM edificacao
                          WHERE ST_DWithin(geom::geography, a.geom_camera::geography, 80)) e) AS edificacoes
            FROM imovel_analise a JOIN foto f ON f.id = a.foto_id JOIN imovel i ON i.id = a.id
            WHERE a.id = %s""", (imovel_id,)).fetchone()
        if not d:
            raise HTTPException(404, "imóvel não encontrado")
        geo(conn, d)
        d["referencias"] = conn.execute("""
            SELECT r.fonte, r.categoria, r.descricao,
                   round(ST_Distance(r.geom::geography, i.geom::geography)) AS distancia_m
            FROM referencia_publica r, imovel i
            WHERE i.id = %s AND ST_DWithin(r.geom::geography, i.geom::geography, 30)
            ORDER BY distancia_m LIMIT 30""", (imovel_id,)).fetchall()
        # Análise direcionada: só o número do alvo e a associação (a matrícula fica fora da Revisão, como o GSAN)
        d["alvos"] = conn.execute("""SELECT f.analise_id, f.alvo_id, f.associacao, a.ambiente FROM analise_foto f
                                     JOIN analise a ON a.id = f.analise_id WHERE f.imovel_id = %s ORDER BY f.analise_id DESC""",
                                  (imovel_id,)).fetchall()
        d["historico"] = conn.execute("""SELECT acao, dados, em FROM historico
                                         WHERE entidade = 'imovel' AND entidade_id = %s ORDER BY id DESC LIMIT 20""",
                                      (imovel_id,)).fetchall()
        d["gsan"] = None
        if d["revisado_em"]:  # GSAN só depois da revisão da classificação, para mantê-la independente
            d["gsan"] = conn.execute("""
                SELECT a.situacao, a.tipo_divergencia, a.associacao, a.matriculas, a.categoria_gsan,
                       a.economias_gsan, a.matriculas_confirmadas,
                       (SELECT coalesce(json_agg(c ORDER BY c.distancia_m), '[]') FROM (
                          SELECT l.matricula, l.endereco, l.categoria, l.economias,
                                 round(ST_Distance(l.geom::geography, a.geom::geography)) AS distancia_m
                          FROM gsan_ligacao l
                          WHERE ST_DWithin(l.geom::geography, a.geom::geography, 40)
                             OR l.matricula = ANY (a.matriculas_confirmadas)) c) AS candidatas
                FROM imovel_analise a WHERE a.id = %s""", (imovel_id,)).fetchone()
    return d


def geo(conn, d):
    """Associação geográfica e multivisual da observação: candidatas, trajetória, grupo, endereços lidos."""
    i = d["id"]
    d["localizacao_evidencias"] = conn.execute("SELECT localizacao_evidencias FROM imovel WHERE id = %s", (i,)).fetchone()["localizacao_evidencias"]
    d["candidatas"] = conn.execute("""SELECT c.edificacao_id, c.pontuacao, c.evidencias, e.fonte FROM candidato_edificacao c
                                      JOIN edificacao e ON e.id = c.edificacao_id WHERE c.imovel_id = %s ORDER BY c.pontuacao DESC""",
                                   (i,)).fetchall()
    d["trajeto"] = conn.execute("""SELECT t.*, f.rumo AS rumo_informado, f.lado_via AS lado_informado, f.precisao_m
                                   FROM foto_trajeto t JOIN foto f ON f.id = t.foto_id WHERE t.foto_id = %s""", (d["foto_id"],)).fetchone()
    from .api_geo import DE_OBSERVACAO, OBSERVACAO

    membros = f"SELECT {OBSERVACAO} FROM {DE_OBSERVACAO}"
    g = conn.execute("SELECT i.grupo_id, g.origem, g.evidencias FROM imovel i JOIN grupo g ON g.id = i.grupo_id WHERE i.id = %s", (i,)).fetchone()
    d["grupo"] = g and {"id": g["grupo_id"], "origem": g["origem"], "evidencias": g["evidencias"],
                        "membros": conn.execute(membros + " WHERE i.grupo_id = %s ORDER BY a.id", (g["grupo_id"],)).fetchall()}
    # Outras fotos que podem mostrar o mesmo imóvel: mesma edificação (escolhida ou candidata) ou fachada a até 25 m
    d["proximas"] = conn.execute(membros + """ WHERE a.foto_id <> %(foto)s AND a.fachada_valida IS DISTINCT FROM false
          AND i.grupo_id IS DISTINCT FROM %(grupo)s
          AND (a.edificacao_id IN (SELECT edificacao_id FROM candidato_edificacao WHERE imovel_id = %(id)s)
               OR ST_DWithin(a.geom::geography, (SELECT geom FROM imovel WHERE id = %(id)s)::geography, 25))
        ORDER BY ST_Distance(a.geom::geography, (SELECT geom FROM imovel WHERE id = %(id)s)::geography) LIMIT 12""",
                                 {"foto": d["foto_id"], "grupo": g and g["grupo_id"], "id": i}).fetchall()
    d["leituras"] = conn.execute("""SELECT l.*, r.fonte AS referencia_fonte FROM leitura_endereco l
                                    LEFT JOIN referencia_publica r ON r.id = l.referencia_id WHERE l.imovel_id = %s ORDER BY l.id""",
                                 (i,)).fetchall()


@app.get("/api/busca")
def busca(q: str = Query(min_length=1, max_length=60)):
    """Imóvel pelo número (#12) ou pela matrícula associada."""
    with conectar() as conn:
        return conn.execute("""SELECT id FROM imovel_analise
                               WHERE id::text = ltrim(%(q)s, '#') OR %(q)s = ANY (matriculas) ORDER BY id LIMIT 20""",
                            {"q": q.strip()}).fetchall()


@app.get("/api/edificacoes")
def edificacoes(bbox: str = Query(pattern=r"^-?[\d.]+,-?[\d.]+,-?[\d.]+,-?[\d.]+$")):
    """Polígonos na área visível do mapa (oeste,sul,leste,norte), no máximo 3000."""
    o, s, l, n = map(float, bbox.split(","))
    with conectar() as conn:
        return conn.execute("""
            SELECT json_build_object('type', 'FeatureCollection', 'features', coalesce(json_agg(ST_AsGeoJSON(e.*)::json), '[]'))
            FROM (SELECT id, fonte, confianca, geom, a.observacoes,
                         CASE WHEN a.manuais > 0 THEN 'confirmada' WHEN a.observacoes > 0 THEN 'sugerida' END AS associacao
                  FROM edificacao
                  LEFT JOIN LATERAL (SELECT count(*) AS observacoes, count(*) FILTER (WHERE localizacao_fonte = 'manual') AS manuais
                                     FROM imovel i WHERE i.edificacao_id = edificacao.id
                                       AND i.fachada_valida IS DISTINCT FROM false) a ON true
                  WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326) LIMIT 3000) e""", (o, s, l, n)).fetchone()["json_build_object"]


@app.get("/api/fotos")
def fotos():
    with conectar() as conn:
        return conn.execute("""
            SELECT f.id, f.arquivo, f.credito, f.capturada_em, f.largura, f.altura, f.detectada, f.direcao,
                   ST_Y(f.geom) AS lat, ST_X(f.geom) AS lon, count(i.id) AS imoveis,
                   t.trajeto, t.ordem, t.rumo, t.rumo_confiavel, t.lado,
                   count(i.id) FILTER (WHERE i.revisado_em IS NULL AND i.fachada_valida IS DISTINCT FROM false) AS pendentes,
                   count(i.id) FILTER (WHERE i.fachada_valida IS DISTINCT FROM false) AS validos,
                   CASE WHEN NOT f.detectada AND count(i.id) = 0 THEN 'sem_deteccao'
                        WHEN count(i.id) FILTER (WHERE i.revisado_em IS NULL AND i.fachada_valida IS DISTINCT FROM false) > 0 THEN 'pendente'
                        WHEN count(i.id) FILTER (WHERE i.fachada_valida IS DISTINCT FROM false) = 0 THEN 'inutilizavel'
                        ELSE 'revisada' END AS estado
            FROM foto f LEFT JOIN imovel i ON i.foto_id = f.id LEFT JOIN foto_trajeto t ON t.foto_id = f.id
            GROUP BY f.id, t.foto_id ORDER BY f.id""").fetchall()


@app.get("/api/fotos/{foto_id}")
def foto(foto_id: int):
    with conectar() as conn:
        f = conn.execute("""SELECT id, arquivo, fonte, credito, capturada_em, largura, altura, detectada, direcao,
                                   fov_horizontal FROM foto WHERE id = %s""", (foto_id,)).fetchone()
        if not f:
            raise HTTPException(404, "foto não encontrada")
        f["imoveis"] = conn.execute("""
            SELECT a.id, a.bbox, a.bbox_detectada, a.detector, a.score_deteccao, a.categoria_ia, a.unidades_ia,
                   a.confianca_ia, a.evidencias_ia, a.limitacoes_ia, a.modelo_ia, a.categoria_revisada,
                   a.unidades_revisadas, a.fachada_valida, a.revisado_em, a.observacao, a.categoria_final,
                   a.unidades_final, a.localizacao_confianca, i.texto_ocr
            FROM imovel_analise a JOIN imovel i ON i.id = a.id WHERE a.foto_id = %s ORDER BY a.id""", (foto_id,)).fetchall()
    return f


@app.get("/api/fotos/{foto_id}/imagem")
def foto_imagem(foto_id: int, w: int = 1600):
    with conectar() as conn:
        f = conn.execute("SELECT arquivo FROM foto WHERE id = %s", (foto_id,)).fetchone()
    if not f:
        raise HTTPException(404, "foto não encontrada")
    return imagem_redimensionada(f["arquivo"], w, f"foto{foto_id}")


class Revisao(BaseModel):
    fachada_valida: bool
    categoria: Categoria | None = None
    unidades: int | None = Field(None, ge=1, le=1000)
    observacao: str | None = Field(None, max_length=1000)


def _revisar(conn, imovel_id, r):
    ok = conn.execute(
        """UPDATE imovel SET fachada_valida = %s, categoria_revisada = %s, unidades_revisadas = %s,
                  observacao = %s, revisado_em = now() WHERE id = %s RETURNING id""",
        (r.fachada_valida, r.categoria, r.unidades, r.observacao, imovel_id)).fetchone()
    if not ok:
        raise HTTPException(404, "imóvel não encontrado")
    registrar(conn, "imovel", imovel_id, "revisao", r.model_dump())


@app.post("/api/imoveis/{imovel_id}/revisao")
def revisar(imovel_id: int, r: Revisao):
    with conectar() as conn:
        _revisar(conn, imovel_id, r)
    return {"ok": True}


class RevisaoFachada(Revisao):
    id: int


class RevisaoFoto(BaseModel):
    imoveis: list[RevisaoFachada] = Field(max_length=200)


@app.post("/api/fotos/{foto_id}/revisao")
def revisar_foto(foto_id: int, r: RevisaoFoto):
    """Aprova (ou descarta) todas as fachadas de uma foto de uma vez, na Revisão de imagens."""
    with conectar() as conn:
        da_foto = {x["id"]: x for x in conn.execute(
            "SELECT id, categoria_ia FROM imovel WHERE foto_id = %s", (foto_id,))}
        for x in r.imoveis:
            if x.id not in da_foto:
                raise HTTPException(422, f"imóvel {x.id} não é desta foto")
            if x.fachada_valida and not x.categoria and not da_foto[x.id]["categoria_ia"]:
                raise HTTPException(422, f"escolha o tipo do imóvel #{x.id}")
            _revisar(conn, x.id, x)
    return {"ok": True}


class Localizacao(BaseModel):
    edificacao_id: int | None = None
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)

    @model_validator(mode="after")
    def um_modo(self):
        if (self.edificacao_id is None) == (self.lat is None or self.lon is None):
            raise ValueError("informe edificacao_id ou lat/lon")
        return self


@app.post("/api/imoveis/{imovel_id}/localizacao")
def localizar_manual(imovel_id: int, l: Localizacao):
    with conectar() as conn:
        if not posicionar_manual(conn, imovel_id, l.edificacao_id, l.lat, l.lon):
            raise HTTPException(404, "imóvel ou edificação não encontrados")
        registrar(conn, "imovel", imovel_id, "localizacao", l.model_dump())
    return {"ok": True}


class Caixa(BaseModel):
    bbox: list[int] = Field(min_length=4, max_length=4)


def _caixa_valida(conn, foto_id, bbox):
    x1, y1, x2, y2 = bbox
    f = conn.execute("SELECT largura, altura FROM foto WHERE id = %s", (foto_id,)).fetchone()
    if not f:
        raise HTTPException(404, "foto não encontrada")
    if not (0 <= x1 < x2 <= f["largura"] and 0 <= y1 < y2 <= f["altura"]):
        raise HTTPException(422, "caixa fora da imagem")


@app.post("/api/imoveis/{imovel_id}/caixa")
def corrigir_caixa(imovel_id: int, c: Caixa):
    """Nova caixa: a classificação da IA é refeita no próximo 'classificar', com o novo recorte."""
    with conectar() as conn:
        m = conn.execute("SELECT foto_id, bbox FROM imovel WHERE id = %s", (imovel_id,)).fetchone()
        if not m:
            raise HTTPException(404, "imóvel não encontrado")
        _caixa_valida(conn, m["foto_id"], c.bbox)
        conn.execute("""UPDATE imovel SET bbox = %s, fachada_valida = true, categoria_ia = NULL, unidades_ia = NULL,
                               confianca_ia = NULL, evidencias_ia = NULL, limitacoes_ia = NULL, texto_ocr = NULL,
                               classificado_em = NULL
                        WHERE id = %s""", (c.bbox, imovel_id))
        registrar(conn, "imovel", imovel_id, "caixa", {"antes": m["bbox"], "depois": c.bbox})
        localizar(conn, imovel_id)  # o centro da caixa muda a direção do raio
    return {"ok": True}


@app.post("/api/fotos/{foto_id}/imoveis")
def nova_fachada(foto_id: int, c: Caixa):
    """Fachada que o detector perdeu, desenhada pelo revisor."""
    with conectar() as conn:
        _caixa_valida(conn, foto_id, c.bbox)
        novo = conn.execute("""INSERT INTO imovel (foto_id, bbox, detector, fachada_valida, geom)
                               SELECT id, %s, 'manual', true, geom FROM foto WHERE id = %s RETURNING id""",
                            (c.bbox, foto_id)).fetchone()
        # Foto marcada pelo revisor não passa mais pelo detector, para não duplicar fachadas
        conn.execute("UPDATE foto SET detectada = true WHERE id = %s", (foto_id,))
        registrar(conn, "imovel", novo["id"], "caixa_manual", {"bbox": c.bbox})
        localizar(conn, novo["id"])
    return {"id": novo["id"]}


class Associacao(BaseModel):
    matriculas: list[str] = Field(max_length=200)  # vazia = revisor confirma que não há matrícula


@app.post("/api/imoveis/{imovel_id}/associacao")
def associar(imovel_id: int, a: Associacao):
    with conectar() as conn:
        m = conn.execute("SELECT revisado_em FROM imovel WHERE id = %s", (imovel_id,)).fetchone()
        if not m:
            raise HTTPException(404, "imóvel não encontrado")
        if not m["revisado_em"]:
            raise HTTPException(409, "revise a classificação antes de associar ao GSAN")
        existentes = {r["matricula"] for r in conn.execute(
            "SELECT matricula FROM gsan_ligacao WHERE matricula = ANY (%s)", (a.matriculas,))}
        if set(a.matriculas) - existentes:
            raise HTTPException(422, f"matrícula(s) inexistente(s): {sorted(set(a.matriculas) - existentes)}")
        conn.execute("UPDATE imovel SET matriculas_confirmadas = %s, associacao_revisada_em = now() WHERE id = %s",
                     (sorted(existentes), imovel_id))
        registrar(conn, "imovel", imovel_id, "associacao", {"matriculas": sorted(existentes)})
    return {"ok": True}


async def corpo_limitado(request: Request, limite):
    """Upload como corpo cru da requisição (sem multipart, que exigiria outra dependência)."""
    dados = await request.body()
    if not dados:
        raise HTTPException(422, "arquivo vazio")
    if len(dados) > limite:
        raise HTTPException(413, f"arquivo maior que {limite // (1024 * 1024)} MB")
    return dados


from .api_geo import rotas as rotas_geo  # noqa: E402
from .api_plataforma import rotas  # noqa: E402  (usa os helpers acima)

app.include_router(rotas)
app.include_router(rotas_geo)
FOTOS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/fotos", StaticFiles(directory=FOTOS_DIR), name="fotos")
app.mount("/", StaticFiles(directory=RAIZ / "web", html=True), name="web")
