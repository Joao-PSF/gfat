"""Rotas geográficas: ficha da edificação, trajetórias, lotes, grupos de observações, unidades e endereços lidos."""
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from . import multivisual
from .db import conectar, registrar
from .enderecos import registrar_leitura

rotas = APIRouter(prefix="/api")
BBOX = r"^-?[\d.]+,-?[\d.]+,-?[\d.]+,-?[\d.]+$"
Categoria = Literal["residencial", "comercial", "industrial", "institucional", "misto", "terreno_vazio", "inconclusivo"]

# Uma observação (caixa numa foto) com o que a interface mostra em listas e miniaturas
OBSERVACAO = """a.id, a.foto_id, f.arquivo, f.largura, f.altura, f.capturada_em, a.bbox, a.edificacao_id, a.localizacao_fonte,
                a.localizacao_confianca, a.categoria_final, ST_Y(a.geom) AS lat, ST_X(a.geom) AS lon,
                ST_Y(f.geom) AS lat_camera, ST_X(f.geom) AS lon_camera, a.unidades_final, a.revisado_em IS NOT NULL AS revisado,
                i.grupo_id, (SELECT origem FROM grupo WHERE id = i.grupo_id) AS grupo_origem"""
# imovel_analise foi criada antes de grupo_id existir: a coluna vem de imovel
DE_OBSERVACAO = "imovel_analise a JOIN imovel i ON i.id = a.id JOIN foto f ON f.id = a.foto_id"


def _geojson(conn, sql, args):
    return conn.execute(f"""SELECT json_build_object('type', 'FeatureCollection', 'features',
                                   coalesce(json_agg(ST_AsGeoJSON(x.*)::json), '[]')) AS g FROM ({sql}) x""", args).fetchone()["g"]


@rotas.get("/trajetos")
def trajetos():
    """Percursos com 2 ou mais fotos, na ordem de captura."""
    with conectar() as conn:
        return _geojson(conn, """SELECT t.trajeto, count(*) AS fotos, ST_MakeLine(f.geom ORDER BY t.ordem) AS geom
                                 FROM foto_trajeto t JOIN foto f ON f.id = t.foto_id GROUP BY t.trajeto HAVING count(*) > 1""", ())


@rotas.get("/lotes")
def lotes(bbox: str = Query(pattern=BBOX)):
    o, s, l, n = map(float, bbox.split(","))
    with conectar() as conn:
        return _geojson(conn, """SELECT id, fonte, id_origem, geom FROM lote
                                 WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326) LIMIT 3000""", (o, s, l, n))


@rotas.get("/edificacoes/{edificacao_id}")
def ficha_edificacao(edificacao_id: int):
    with conectar() as conn:
        e = conn.execute("""SELECT id, fonte, id_origem, confianca, round(ST_Area(geom::geography)::numeric, 1)::float AS area_m2,
                                   ST_AsGeoJSON(geom)::json AS geometria FROM edificacao WHERE id = %s""", (edificacao_id,)).fetchone()
        if not e:
            raise HTTPException(404, "edificação não encontrada")
        e["lotes"] = conn.execute("""
            SELECT l.id, l.fonte, l.id_origem,
                   round((ST_Area(ST_Intersection(l.geom, e.geom)::geography) / ST_Area(e.geom::geography) * 100)::numeric)::int AS pct_edificacao
            FROM lote l, edificacao e WHERE e.id = %s AND ST_Intersects(l.geom, e.geom) ORDER BY pct_edificacao DESC""",
                                  (edificacao_id,)).fetchall()
        e["observacoes"] = conn.execute(f"""
            SELECT {OBSERVACAO}, c.pontuacao, c.evidencias FROM {DE_OBSERVACAO}
            LEFT JOIN candidato_edificacao c ON c.imovel_id = a.id AND c.edificacao_id = a.edificacao_id
            WHERE a.edificacao_id = %s AND a.fachada_valida IS DISTINCT FROM false ORDER BY i.grupo_id NULLS LAST, a.id""",
                                        (edificacao_id,)).fetchall()
        e["candidatas"] = conn.execute(f"""
            SELECT {OBSERVACAO}, c.pontuacao, c.evidencias FROM candidato_edificacao c
            JOIN {DE_OBSERVACAO} ON a.id = c.imovel_id
            WHERE c.edificacao_id = %s AND a.edificacao_id IS DISTINCT FROM %s AND a.fachada_valida IS DISTINCT FROM false
            ORDER BY c.pontuacao DESC""", (edificacao_id, edificacao_id)).fetchall()
        e["unidades"] = conn.execute("""SELECT id, grupo_id, rotulo, categoria, numero, criada_em FROM unidade
                                        WHERE edificacao_id = %s ORDER BY id""", (edificacao_id,)).fetchall()
        e["leituras"] = conn.execute("""
            SELECT l.*, r.fonte AS referencia_fonte FROM leitura_endereco l JOIN imovel i ON i.id = l.imovel_id
            LEFT JOIN referencia_publica r ON r.id = l.referencia_id
            WHERE i.edificacao_id = %s ORDER BY l.estado = 'rejeitada', l.id""", (edificacao_id,)).fetchall()
        e["enderecos_publicos"] = conn.execute("""
            SELECT r.id, r.fonte, r.logradouro, r.numero, r.descricao,
                   round(ST_Distance(r.geom::geography, e.geom::geography)::numeric, 1)::float AS distancia_m
            FROM referencia_publica r, edificacao e
            WHERE e.id = %s AND r.numero IS NOT NULL AND ST_DWithin(r.geom::geography, e.geom::geography, 15)
            ORDER BY distancia_m LIMIT 20""", (edificacao_id,)).fetchall()
        e["historico"] = conn.execute("""SELECT acao, dados, em FROM historico WHERE entidade = 'edificacao' AND entidade_id = %s
                                         ORDER BY id DESC LIMIT 20""", (edificacao_id,)).fetchall()
    return e


class Reunir(BaseModel):
    imoveis: list[int] = Field(min_length=2, max_length=50)


@rotas.post("/grupos")
def reunir(r: Reunir):
    """Revisor diz que estas observações mostram o mesmo imóvel."""
    with conectar() as conn:
        try:
            gid, membros = multivisual.reunir(conn, r.imoveis)
        except LookupError as e:
            raise HTTPException(404, str(e))
        for i in membros:
            registrar(conn, "imovel", i, "grupo_reunir", {"grupo": gid, "pedido": r.imoveis, "membros": membros})
    return {"id": gid, "membros": membros}


@rotas.post("/imoveis/{imovel_id}/separar")
def separar(imovel_id: int):
    with conectar() as conn:
        try:
            antes, gid = multivisual.separar(conn, imovel_id)
        except LookupError as e:
            raise HTTPException(404, str(e))
        registrar(conn, "imovel", imovel_id, "grupo_separar", {"grupo_anterior": antes, "grupo": gid})
    return {"id": gid}


@rotas.post("/grupos/{grupo_id}/confirmar")
def confirmar_grupo(grupo_id: int):
    """Grupo sugerido passa a ser do revisor: a sugestão automática não o refaz mais."""
    with conectar() as conn:
        membros = [r["id"] for r in conn.execute("SELECT id FROM imovel WHERE grupo_id = %s", (grupo_id,))]
        if not conn.execute("UPDATE grupo SET origem = 'manual' WHERE id = %s RETURNING id", (grupo_id,)).fetchone():
            raise HTTPException(404, "grupo não encontrado")
        for i in membros:
            registrar(conn, "imovel", i, "grupo_confirmar", {"grupo": grupo_id, "membros": membros})
    return {"ok": True}


class NovaUnidade(BaseModel):
    rotulo: str = Field(min_length=1, max_length=120)
    categoria: Categoria | None = None
    numero: str | None = Field(None, max_length=20)
    grupo_id: int | None = None


@rotas.post("/edificacoes/{edificacao_id}/unidades")
def nova_unidade(edificacao_id: int, u: NovaUnidade):
    with conectar() as conn:
        if not conn.execute("SELECT 1 FROM edificacao WHERE id = %s", (edificacao_id,)).fetchone():
            raise HTTPException(404, "edificação não encontrada")
        if u.grupo_id and not conn.execute("SELECT 1 FROM grupo WHERE id = %s", (u.grupo_id,)).fetchone():
            raise HTTPException(422, "grupo inexistente")
        uid = conn.execute("""INSERT INTO unidade (edificacao_id, grupo_id, rotulo, categoria, numero)
                              VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                           (edificacao_id, u.grupo_id, u.rotulo.strip(), u.categoria, u.numero)).fetchone()["id"]
        registrar(conn, "edificacao", edificacao_id, "unidade_criada", {"unidade": uid, **u.model_dump()})
    return {"id": uid}


@rotas.delete("/unidades/{unidade_id}")
def remover_unidade(unidade_id: int):
    with conectar() as conn:
        u = conn.execute("DELETE FROM unidade WHERE id = %s RETURNING *", (unidade_id,)).fetchone()
        if not u:
            raise HTTPException(404, "unidade não encontrada")
        registrar(conn, "edificacao", u["edificacao_id"], "unidade_removida",
                  {k: u[k] for k in ("id", "rotulo", "categoria", "numero", "grupo_id")})
    return {"ok": True}


class NovaLeitura(BaseModel):
    numero: str = Field(min_length=1, max_length=20)
    texto: str | None = Field(None, max_length=500)


@rotas.post("/imoveis/{imovel_id}/leituras")
def nova_leitura(imovel_id: int, n: NovaLeitura):
    """Número digitado pelo revisor (a placa está legível na foto); já entra confirmado."""
    with conectar() as conn:
        if not conn.execute("SELECT 1 FROM imovel WHERE id = %s", (imovel_id,)).fetchone():
            raise HTTPException(404, "imóvel não encontrado")
        try:
            lid = registrar_leitura(conn, imovel_id, n.numero, "manual", n.texto)
        except ValueError as e:
            raise HTTPException(422, str(e))
        conn.execute("UPDATE leitura_endereco SET estado = 'confirmada', revisada_em = now() WHERE id = %s", (lid,))
        registrar(conn, "imovel", imovel_id, "leitura_manual", {"leitura": lid, "numero": n.numero})
    return {"id": lid}


class RevisaoLeitura(BaseModel):
    estado: Literal["confirmada", "rejeitada", "pendente"]


@rotas.post("/leituras/{leitura_id}")
def revisar_leitura(leitura_id: int, r: RevisaoLeitura):
    with conectar() as conn:
        l = conn.execute("""UPDATE leitura_endereco SET estado = %s, revisada_em = CASE WHEN %s = 'pendente' THEN NULL ELSE now() END
                            WHERE id = %s RETURNING imovel_id, numero""", (r.estado, r.estado, leitura_id)).fetchone()
        if not l:
            raise HTTPException(404, "leitura não encontrada")
        registrar(conn, "imovel", l["imovel_id"], "leitura_revisao",
                  {"leitura": leitura_id, "numero": l["numero"], "estado": r.estado})
    return {"ok": True}
