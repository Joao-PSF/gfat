"""Associação multivisual: quais observações (caixas em fotos diferentes) mostram o mesmo imóvel.

Cada fonte de evidência devolve pares (observação a, observação b, evidência). Hoje só existe a geográfica;
correspondência visual, OCR e continuidade arquitetônica entram como novas funções em FONTES, sem mudar o
armazenamento (grupo, imovel.grupo_id). Grupos 'manual' (feitos ou confirmados pelo revisor) nunca são
alterados aqui, e os 'sugerido' são refeitos a cada execução."""
from psycopg.types.json import Jsonb

DIST_MAX_M = 10  # pontos de fachada mais distantes que isso não são agrupados automaticamente


def pares_geograficos(conn):
    """Mesma edificação sugerida, fotos diferentes, pontos de fachada próximos. Duas caixas da mesma foto são
    fachadas diferentes, salvo decisão do revisor."""
    return [(r["a"], r["b"], {"fonte": "geografica", "edificacao_id": r["edificacao_id"], "distancia_m": r["d"]})
            for r in conn.execute("""
        SELECT a.id AS a, b.id AS b, a.edificacao_id, round(ST_Distance(a.geom::geography, b.geom::geography)::numeric, 1)::float AS d
        FROM imovel a JOIN imovel b ON a.id < b.id AND a.foto_id <> b.foto_id AND a.edificacao_id = b.edificacao_id
        WHERE ST_DWithin(a.geom::geography, b.geom::geography, %s)
          AND a.fachada_valida IS DISTINCT FROM false AND b.fachada_valida IS DISTINCT FROM false""", (DIST_MAX_M,))]


FONTES = [pares_geograficos]


def sugerir_grupos(conn):
    """Refaz os grupos sugeridos. Ligação completa: uma observação só entra num grupo se tiver evidência com
    todos os membros (evita encadear lojas vizinhas de um mesmo polígono). Retorna quantos grupos sugeriu."""
    conn.execute("UPDATE imovel SET grupo_id = NULL WHERE grupo_id IN (SELECT id FROM grupo WHERE origem = 'sugerido')")
    conn.execute("DELETE FROM grupo WHERE origem = 'sugerido'")
    fixos = {r["id"] for r in conn.execute("SELECT id FROM imovel WHERE grupo_id IS NOT NULL")}
    pares = {}
    for fonte in FONTES:
        for a, b, ev in fonte(conn):
            if a not in fixos and b not in fixos:
                pares.setdefault((a, b), []).append(ev)
    grupo_de, grupos = {}, {}
    for (a, b) in sorted(pares, key=lambda p: min(e.get("distancia_m", 0) for e in pares[p])):
        ga, gb = grupo_de.get(a, frozenset([a])), grupo_de.get(b, frozenset([b]))
        if ga is gb or not all((min(x, y), max(x, y)) in pares for x in ga for y in gb):
            continue
        novo = ga | gb
        for x in novo:
            grupo_de[x] = novo
    for membros in {g for g in grupo_de.values()}:
        evid = [{"a": a, "b": b, "evidencias": pares[(a, b)]} for a in membros for b in membros if (a, b) in pares]
        gid = conn.execute("INSERT INTO grupo (origem, evidencias) VALUES ('sugerido', %s) RETURNING id",
                           (Jsonb({"pares": evid}),)).fetchone()["id"]
        conn.execute("UPDATE imovel SET grupo_id = %s WHERE id = ANY (%s)", (gid, sorted(membros)))
        grupos[gid] = membros
    return len(grupos)


def _limpar(conn):
    """Grupo sugerido com menos de 2 observações não diz nada; grupo vazio some. Manual de 1 = isolada pelo revisor."""
    conn.execute("""UPDATE imovel SET grupo_id = NULL WHERE grupo_id IN (
                      SELECT g.id FROM grupo g WHERE g.origem = 'sugerido'
                      AND (SELECT count(*) FROM imovel i WHERE i.grupo_id = g.id) < 2)""")
    conn.execute("DELETE FROM grupo g WHERE NOT EXISTS (SELECT 1 FROM imovel i WHERE i.grupo_id = g.id)")


def reunir(conn, ids):
    """Junta as observações, e os grupos a que já pertencem, num grupo do revisor. Retorna (grupo, membros)."""
    achadas = conn.execute("SELECT id, grupo_id FROM imovel WHERE id = ANY (%s)", (list(ids),)).fetchall()
    if len(achadas) != len(set(ids)):
        raise LookupError("observação inexistente")
    antigos = [r["grupo_id"] for r in achadas if r["grupo_id"]]
    membros = sorted(set(ids) | {r["id"] for r in conn.execute("SELECT id FROM imovel WHERE grupo_id = ANY (%s)", (antigos,))})
    gid = conn.execute("INSERT INTO grupo (origem, evidencias) VALUES ('manual', %s) RETURNING id",
                       (Jsonb({"revisor": "reunidas", "observacoes": sorted(set(ids)), "grupos_anteriores": antigos}),)).fetchone()["id"]
    conn.execute("UPDATE imovel SET grupo_id = %s WHERE id = ANY (%s)", (gid, membros))
    _limpar(conn)
    return gid, membros


def separar(conn, imovel_id):
    """Tira a observação do grupo. Ela fica num grupo manual só dela, para a sugestão automática não a juntar de novo."""
    m = conn.execute("SELECT grupo_id FROM imovel WHERE id = %s", (imovel_id,)).fetchone()
    if not m:
        raise LookupError("observação inexistente")
    gid = conn.execute("INSERT INTO grupo (origem, evidencias) VALUES ('manual', %s) RETURNING id",
                       (Jsonb({"revisor": "separada", "grupo_anterior": m["grupo_id"]}),)).fetchone()["id"]
    conn.execute("UPDATE imovel SET grupo_id = %s WHERE id = %s", (gid, imovel_id))
    _limpar(conn)
    return m["grupo_id"], gid
