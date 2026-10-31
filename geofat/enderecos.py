"""Números de endereço lidos nas observações (OCR configurável ou digitados pelo revisor) e o endereço candidato
nos dados públicos (CNEFE e OSM) perto da edificação. Toda leitura entra pendente de revisão."""
import re
import time

from psycopg.types.json import Jsonb

from .config import FOTOS_DIR
from .tarefas import ContextoConsole

RAIO_M = 30  # pontos do CNEFE ficam na testada do lote; o GPS da foto erra alguns metros

# Telefones, datas, horas, preços e medidas não são números de casa
_RUIDO = re.compile(r"\(\d{2}\)|\d+(?:[.,:/]\d+)+|\d{4,5}\s?-\s?\d{4}|R\$\s*\d+(?:[.,]\d+)*|\d+\s?(?:m²|m2|km|kg|ml|%)", re.IGNORECASE)
_NUMERO = re.compile(r"(?<![\w-])(\d{1,5})(?:\s?-?\s?([A-Z]))?(?![\w-])")


def normalizar(numero):
    """'0218-a' -> '218A'; sem número (S/N, 0) -> None."""
    n = re.sub(r"[^0-9A-Z]", "", (numero or "").upper()).lstrip("0")
    return n if n and n[0].isdigit() else None


def numeros(texto):
    """Números de endereço possíveis num texto de OCR, na ordem em que aparecem, sem repetição."""
    if not texto or texto.strip().upper() == "NENHUM":
        return []
    limpo = _RUIDO.sub(" ", texto)
    vistos = []
    for m in _NUMERO.finditer(limpo):
        n = normalizar(m.group(1) + (m.group(2) or ""))
        if n and n not in vistos:
            vistos.append(n)
    return vistos


def candidato(conn, imovel_id, numero):
    """Endereços públicos com o mesmo número perto da edificação da observação (ou do ponto, sem edificação)."""
    return conn.execute("""
        SELECT r.id, r.fonte, r.logradouro, r.numero, r.descricao,
               round(ST_Distance(r.geom::geography, coalesce(e.geom, i.geom)::geography)::numeric, 1)::float AS distancia_m
        FROM imovel i LEFT JOIN edificacao e ON e.id = i.edificacao_id, referencia_publica r
        WHERE i.id = %s AND r.numero = %s AND ST_DWithin(r.geom::geography, coalesce(e.geom, i.geom)::geography, %s)
        ORDER BY distancia_m, r.fonte LIMIT 5""", (imovel_id, numero, RAIO_M)).fetchall()


def registrar_leitura(conn, imovel_id, numero, origem, texto=None, modelo=None, bbox=None):
    n = normalizar(numero)
    if not n:
        raise ValueError("número inválido")
    refs = candidato(conn, imovel_id, n)
    melhor = refs[0] if refs else None
    endereco = ", ".join(filter(None, (melhor["logradouro"], melhor["numero"]))) if melhor else None
    ev = {"referencias": refs, "raio_m": RAIO_M}
    if origem == "ocr":
        ev["regiao"] = "recorte da fachada (o provedor não informa a posição do texto)"
    return conn.execute("""INSERT INTO leitura_endereco (imovel_id, origem, modelo, texto_original, numero, bbox, evidencias,
                                                         endereco_candidato, referencia_id)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                        (imovel_id, origem, modelo, texto, n, bbox, Jsonb(ev), endereco,
                         melhor["id"] if melhor else None)).fetchone()["id"]


def ler_enderecos(conn, ler, nome_modelo, limite=None, ctx=None):
    """Lê o texto das observações válidas sem OCR (guarda em imovel.texto_ocr, que a troca de caixa apaga) e
    registra os números encontrados. Texto já lido na classificação só é reaproveitado. Retorna (lidas, numeros, falhas)."""
    from .classificar import recortar
    from .fotos import abrir

    ctx = ctx or ContextoConsole()
    pendentes = conn.execute("""
        SELECT i.id, i.bbox, i.texto_ocr, f.arquivo FROM imovel i JOIN foto f ON f.id = i.foto_id
        WHERE i.fachada_valida IS DISTINCT FROM false
          AND (i.texto_ocr IS NULL OR NOT EXISTS (SELECT 1 FROM leitura_endereco l WHERE l.imovel_id = i.id AND l.origem = 'ocr'))
        ORDER BY i.texto_ocr IS NULL, i.id LIMIT %s""", (limite,)).fetchall()
    lidas, total, falhas, inicio = 0, 0, [], time.time()
    ctx.progresso(0, len(pendentes), unidade="fachadas")
    for k, p in enumerate(pendentes):
        ctx.verificar()
        ctx.progresso(k, len(pendentes))
        texto, modelo = p["texto_ocr"], "texto lido na classificação"
        if texto is None:
            try:
                texto, modelo = ler(recortar(abrir(FOTOS_DIR / p["arquivo"]), p["bbox"])).strip(), nome_modelo
            except Exception as e:
                falhas.append(f"imóvel {p['id']}: {e}")
                continue
            # Só grava se a caixa não mudou durante a leitura
            if not conn.execute("UPDATE imovel SET texto_ocr = %s WHERE id = %s AND bbox IS NOT DISTINCT FROM %s::int[]",
                                (texto, p["id"], p["bbox"])).rowcount:
                conn.commit()
                falhas.append(f"imóvel {p['id']}: caixa alterada durante a leitura")
                continue
        achados = numeros(texto)
        for n in achados:
            registrar_leitura(conn, p["id"], n, "ocr", texto[:2000], modelo, p["bbox"])
        conn.commit()
        lidas, total = lidas + 1, total + len(achados)
        if modelo == nome_modelo:
            ctx.log(f"imóvel {p['id']}: {', '.join(achados) or 'nenhum número'} ({time.time() - inicio:.0f} s)")
            inicio = time.time()
    ctx.progresso(len(pendentes), len(pendentes))
    return lidas, total, falhas
