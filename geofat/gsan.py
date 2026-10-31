"""Importação da exportação do GSAN. Só a comparação final (view imovel_analise) usa esses dados."""
import csv

COLUNAS = ("matricula", "latitude", "longitude", "endereco", "economias_residencial",
           "economias_comercial", "economias_industrial", "economias_publica")


def importar_gsan(conn, caminho, encoding="utf-8-sig"):
    """CSV com as COLUNAS acima (separador ';' ou ','). Retorna (importados, ignorados)."""
    linhas, ignorados = [], []
    with open(caminho, encoding=encoding, newline="") as f:
        dialeto = csv.Sniffer().sniff(f.readline(), ";,")
        f.seek(0)
        leitor = csv.DictReader(f, dialect=dialeto)
        faltando = set(COLUNAS) - set(leitor.fieldnames or [])
        if faltando:
            raise ValueError(f"CSV do GSAN sem as colunas: {', '.join(sorted(faltando))}")
        for n, r in enumerate(leitor, start=2):
            try:
                lat = float(r["latitude"].replace(",", "."))
                lon = float(r["longitude"].replace(",", "."))
                economias = [int(r[c] or 0) for c in COLUNAS[4:]]
                if not r["matricula"].strip() or min(economias) < 0:
                    raise ValueError
            except (ValueError, AttributeError):
                ignorados.append(n)  # sem coordenada ou número inválido
                continue
            linhas.append((r["matricula"].strip(), r["endereco"], *economias, lon, lat))
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO gsan_ligacao (matricula, endereco, economias_residencial, economias_comercial,
                                         economias_industrial, economias_publica, geom)
               VALUES (%s, %s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
               ON CONFLICT (matricula) DO UPDATE SET
                 endereco = EXCLUDED.endereco, economias_residencial = EXCLUDED.economias_residencial,
                 economias_comercial = EXCLUDED.economias_comercial,
                 economias_industrial = EXCLUDED.economias_industrial,
                 economias_publica = EXCLUDED.economias_publica, geom = EXCLUDED.geom""",
            linhas,
        )
    conn.commit()
    return len(linhas), ignorados


SITUACOES_RELATORIO = ("divergencia_confirmada", "sem_matricula_confirmada", "possivel_divergencia", "sem_correspondencia")


def escrever_relatorio(conn, saida, ids=None, somente_confirmadas=False):
    """CSV (UTF-8 com BOM, para o Excel) dos imóveis com divergência para a equipe comercial.
    `saida` é um arquivo binário; `ids` restringe a uma lista filtrada na interface."""
    filtro = "a.situacao = ANY (%(situacoes)s)"
    if somente_confirmadas:
        filtro += " AND d.decisao = 'confirmada'"
    if ids is not None:
        filtro += " AND a.id = ANY (%(ids)s)"
    sql = f"""COPY (SELECT a.id, a.situacao, a.tipo_divergencia, d.decisao AS decisao_analista, d.observacao AS observacao_analista,
                          a.categoria_final, a.unidades_final, array_to_string(a.matriculas, ' ') AS matriculas, a.associacao,
                          a.categoria_gsan, a.economias_gsan, a.localizacao_fonte, a.localizacao_confianca,
                          round(ST_Y(a.geom)::numeric, 6) AS latitude_fachada, round(ST_X(a.geom)::numeric, 6) AS longitude_fachada,
                          round(ST_Y(a.geom_camera)::numeric, 6) AS latitude_camera, round(ST_X(a.geom_camera)::numeric, 6) AS longitude_camera,
                          f.arquivo AS foto, a.revisado_em IS NOT NULL AS classificacao_revisada,
                          a.evidencias_ia, a.limitacoes_ia, a.observacao
                   FROM imovel_analise a JOIN foto f ON f.id = a.foto_id
                   LEFT JOIN decisao_comercial d ON d.imovel_id = a.id
                   WHERE {filtro}
                   ORDER BY array_position(%(situacoes)s, a.situacao), a.id
                  ) TO STDOUT WITH (FORMAT csv, HEADER)"""
    saida.write(b"\xef\xbb\xbf")  # BOM: o Excel só reconhece UTF-8 com ele
    with conn.cursor().copy(sql, {"situacoes": list(SITUACOES_RELATORIO), "ids": ids}) as copia:
        for bloco in copia:
            saida.write(bloco)
