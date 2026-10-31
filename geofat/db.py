import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import DATABASE_URL, RAIZ


def conectar(url=DATABASE_URL):
    return psycopg.connect(url, row_factory=dict_row)


def migrar(conn):
    """Aplica as migrações de db/migracoes que ainda não rodaram neste banco (ou schema). Preserva os dados."""
    conn.execute("SELECT pg_advisory_xact_lock(4207)")  # API e linha de comando não migram ao mesmo tempo
    conn.execute("CREATE TABLE IF NOT EXISTS migracao (nome text PRIMARY KEY, aplicada_em timestamptz NOT NULL DEFAULT now())")
    feitas = {r["nome"] for r in conn.execute("SELECT nome FROM migracao")}
    novas = []
    for arquivo in sorted((RAIZ / "db" / "migracoes").glob("*.sql")):
        if arquivo.name not in feitas:
            conn.execute(arquivo.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO migracao (nome) VALUES (%s)", (arquivo.name,))
            novas.append(arquivo.name)
    from .provedores import semear

    semear(conn)
    conn.commit()
    return novas


def registrar(conn, entidade, entidade_id, acao, dados=None):
    """Histórico das alterações manuais; entra na mesma transação da alteração."""
    conn.execute("INSERT INTO historico (entidade, entidade_id, acao, dados) VALUES (%s, %s, %s, %s)",
                 (entidade, entidade_id, acao, Jsonb(dados) if dados is not None else None))
