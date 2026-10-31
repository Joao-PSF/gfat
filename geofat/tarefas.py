"""Fila de tarefas em segundo plano (detecção, classificação, downloads, importações, treino), com progresso
real gravado no banco. Cada fila roda uma tarefa por vez numa thread do servidor web."""
import threading
import time
import traceback

from psycopg.types.json import Jsonb

from .db import conectar

FILAS = ("computacao", "rede", "interativa")
TIPOS = {}  # tipo -> (fila, função(ctx, **parametros))


class Cancelada(Exception):
    pass


class Contexto:
    """O que uma tarefa usa para informar progresso, registrar log e saber se foi cancelada."""

    def __init__(self, tarefa_id):
        self.id = tarefa_id
        self.conn = conectar()
        self.conn.autocommit = True  # progresso visível na hora, fora da transação da tarefa
        self._escrito = self._lido = 0.0

    def progresso(self, atual, total=None, mensagem=None, unidade=None):
        agora = time.time()
        if agora - self._escrito < 0.5 and atual != total:
            return
        self._escrito = agora
        self.conn.execute("""UPDATE tarefa SET atual = %s, total = coalesce(%s, total),
                                    mensagem = coalesce(%s, mensagem), unidade = coalesce(%s, unidade) WHERE id = %s""",
                          (atual, total, mensagem, unidade, self.id))

    def log(self, mensagem):
        self.conn.execute(
            """UPDATE tarefa SET log = CASE WHEN jsonb_array_length(log) >= 500 THEN log - 0 ELSE log END
                                       || jsonb_build_array(jsonb_build_object('t', to_char(now(), 'HH24:MI:SS'), 'msg', %s::text))
               WHERE id = %s""", (str(mensagem)[:2000], self.id))

    def verificar(self):
        """Interrompe a tarefa (exceção Cancelada) se o usuário pediu o cancelamento."""
        agora = time.time()
        if agora - self._lido < 1:
            return
        self._lido = agora
        if self.conn.execute("SELECT cancelar FROM tarefa WHERE id = %s", (self.id,)).fetchone()["cancelar"]:
            raise Cancelada()


class ContextoConsole:
    """Mesmo papel do Contexto, para a linha de comando."""

    def progresso(self, atual, total=None, mensagem=None, unidade=None):
        pass

    def log(self, mensagem):
        print(mensagem)

    def verificar(self):
        pass


def tarefa(tipo, fila="computacao"):
    def registrar(funcao):
        TIPOS[tipo] = (fila, funcao)
        return funcao
    return registrar


def enfileirar(conn, tipo, titulo, **parametros):
    fila, _ = TIPOS[tipo]
    return conn.execute("INSERT INTO tarefa (tipo, fila, titulo, parametros) VALUES (%s, %s, %s, %s) RETURNING id",
                        (tipo, fila, titulo, Jsonb(parametros))).fetchone()["id"]


def cancelar(conn, tarefa_id):
    """Na fila: cancela na hora. Executando: pede a parada, que acontece no próximo ponto de verificação."""
    conn.execute("""UPDATE tarefa SET estado = CASE WHEN estado = 'fila' THEN 'cancelada' ELSE estado END,
                           terminada_em = CASE WHEN estado = 'fila' THEN now() ELSE terminada_em END,
                           cancelar = true
                    WHERE id = %s AND estado IN ('fila', 'executando')""", (tarefa_id,))


def repetir(conn, tarefa_id):
    return conn.execute("""INSERT INTO tarefa (tipo, fila, titulo, parametros, repete)
                           SELECT tipo, fila, titulo, parametros, id FROM tarefa
                           WHERE id = %s AND estado IN ('falhou', 'cancelada') RETURNING id""",
                        (tarefa_id,)).fetchone()


def rodar(t):
    """Executa uma tarefa já marcada como 'executando' e grava o resultado."""
    ctx = Contexto(t["id"])
    estado, mensagem, resultado, arquivo = "concluida", None, None, None
    try:
        _, funcao = TIPOS[t["tipo"]]
        resultado = funcao(ctx, **t["parametros"]) or {}
        mensagem, arquivo = resultado.pop("mensagem", None), resultado.pop("arquivo", None)
    except Cancelada:
        estado, mensagem = "cancelada", "Cancelada pelo usuário"
    except Exception as e:
        estado, mensagem = "falhou", f"{type(e).__name__}: {e}"[:1000]
        ctx.log(traceback.format_exc()[-1500:])
    ctx.conn.execute("""UPDATE tarefa SET estado = %s, mensagem = coalesce(%s, mensagem), resultado = %s, arquivo = %s,
                               terminada_em = now() WHERE id = %s""",
                     (estado, mensagem, Jsonb(resultado) if resultado else None, arquivo, t["id"]))
    ctx.conn.close()
    return estado


def _executor(fila, parar):
    while not parar.is_set():
        try:
            with conectar() as conn:
                t = conn.execute("""UPDATE tarefa SET estado = 'executando', iniciada_em = now()
                                    WHERE id = (SELECT id FROM tarefa WHERE estado = 'fila' AND fila = %s
                                                ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
                                    RETURNING *""", (fila,)).fetchone()
        except Exception:
            parar.wait(5)  # banco fora do ar: tenta de novo
            continue
        if t:
            rodar(t)
        else:
            parar.wait(1)


def iniciar_executores():
    """Chamado na subida da API. Tarefas que estavam rodando quando o servidor caiu ficam como falha."""
    with conectar() as conn:
        conn.execute("""UPDATE tarefa SET estado = 'falhou', terminada_em = now(),
                               mensagem = 'Interrompida: o servidor foi reiniciado durante a execução'
                        WHERE estado = 'executando'""")
    parar = threading.Event()
    for fila in FILAS:
        threading.Thread(target=_executor, args=(fila, parar), name=f"tarefas-{fila}", daemon=True).start()
    return parar


# ---- Tipos de tarefa. Cada uma usa a lógica que já existe no pipeline. ----

@tarefa("importar_fotos", "rede")
def _importar_fotos(ctx, pasta=None, fonte="existente"):
    from .config import FOTOS_DIR
    from .fotos import importar_fotos

    with conectar() as conn:
        novas, sem_gps, erros = importar_fotos(conn, fonte, FOTOS_DIR / pasta if pasta else None)
    for x in sem_gps:
        ctx.log(f"sem GPS, ignorada: {x}")
    for x in erros:
        ctx.log(f"erro: {x}")
    return {"mensagem": f"{novas} foto(s) nova(s); {len(sem_gps)} sem GPS; {len(erros)} com erro",
            "novas": novas}


@tarefa("importar_edificacoes", "rede")
def _importar_edificacoes(ctx, fonte="osm"):
    from .edificacoes import importar_open_buildings, importar_osm
    from .localizar import localizar

    ctx.log("Baixando polígonos " + ("do OpenStreetMap" if fonte == "osm" else "do Google Open Buildings"))
    with conectar() as conn:
        n = importar_osm(conn) if fonte == "osm" else importar_open_buildings(conn)
        c = localizar(conn)
    return {"mensagem": f"{n} edificação(ões) importada(s); {c['direcao']} fachada(s) localizada(s) pela direção da câmera"}


@tarefa("localizar")
def _localizar(ctx):
    from .localizar import localizar

    with conectar() as conn:
        c = localizar(conn)
    return {"mensagem": f"{c['direcao']} pela direção da câmera (confiança média), {c['lado_via']} pelo lado da via "
                        f"(baixa), {c['camera']} sem edificação candidata (na câmera)"}


@tarefa("ler_enderecos")
def _ler_enderecos(ctx, limite=None):
    from .enderecos import ler_enderecos
    from .provedores import modelo_da_etapa, obter_ocr

    with conectar() as conn:
        m = modelo_da_etapa(conn, "ocr")
        ctx.log(f"OCR: {m['nome']}")
        lidas, numeros, falhas = ler_enderecos(conn, obter_ocr(m), m["nome"], limite, ctx)
    for f in falhas:
        ctx.log(f)
    return {"mensagem": f"{lidas} fachada(s) lida(s), {numeros} número(s) para revisar"
                        + (f"; {len(falhas)} falha(s)" if falhas else "")}


@tarefa("detectar")
def _detectar(ctx, limite=None):
    from .detectar import detectar
    from .provedores import modelo_da_etapa, obter_detector

    with conectar() as conn:
        m = modelo_da_etapa(conn, "deteccao")
        ctx.log(f"Detector: {m['nome']}")
        fotos, fachadas = detectar(conn, obter_detector(m), m["nome"], limite, ctx)
    return {"mensagem": f"{fotos} foto(s) processada(s), {fachadas} fachada(s) encontrada(s)"}


@tarefa("classificar")
def _classificar(ctx, limite=None):
    from .classificar import classificar
    from .provedores import modelo_da_etapa, obter_classificador, obter_ocr

    with conectar() as conn:
        m = modelo_da_etapa(conn, "classificacao")
        mo = modelo_da_etapa(conn, "ocr", obrigatorio=False)
        ctx.log(f"Classificador: {m['nome']}" + (f"; OCR: {mo['nome']}" if mo else ""))
        perguntar, nome = obter_classificador(m)
        ok, falhas = classificar(conn, limite, perguntar, nome, ctx, obter_ocr(mo) if mo else None)
    for x in falhas:
        ctx.log(f"pendente: {x}")
    return {"mensagem": f"{ok} imóvel(is) classificado(s), {len(falhas)} pendente(s)"}


@tarefa("importar_gsan", "rede")
def _importar_gsan(ctx, arquivo, encoding="utf-8-sig"):
    from .config import RAIZ
    from .db import registrar
    from .gsan import importar_gsan

    with conectar() as conn:
        n, ignorados = importar_gsan(conn, RAIZ / "data" / arquivo, encoding)
        registrar(conn, "gsan", 0, "importacao", {"arquivo": arquivo, "ligacoes": n, "ignoradas": len(ignorados)})
    if ignorados:
        ctx.log(f"{len(ignorados)} linha(s) ignorada(s) (sem coordenada ou valor inválido): {ignorados[:50]}")
    return {"mensagem": f"{n} ligação(ões) lida(s), {len(ignorados)} ignorada(s). O GSAN não foi alterado."}


@tarefa("baixar_modelo", "rede")
def _baixar_modelo(ctx, item):
    from .catalogo import instalar

    return instalar(ctx, item)


@tarefa("sugerir_anotacoes", "interativa")
def _sugerir(ctx, dataset_id, imagem_ids, modelo_id, limiar=None):
    from .datasets import sugerir

    with conectar() as conn:
        return sugerir(conn, ctx, dataset_id, imagem_ids, modelo_id, limiar)


@tarefa("importar_mapillary", "rede")
def _importar_mapillary(ctx, bbox, limite, dataset_id=None, incluir_pano=False):
    from .mapillary import importar

    with conectar() as conn:
        token = conn.execute("SELECT chave FROM provedor WHERE tipo = 'mapillary'").fetchone()["chave"]
        return importar(conn, token, bbox, limite, dataset_id, incluir_pano, ctx)


@tarefa("prerotular")
def _prerotular(ctx, prerotulagem_id):
    from .prerotulagem import executar

    with conectar() as conn:
        return executar(conn, ctx, prerotulagem_id)


@tarefa("importar_alvos", "rede")
def _importar_alvos(ctx, arquivo, origem, quantidade, semente=1, localidade=None, matriculas=None, exigir_foto=True,
                    raio_m=60, apagar=False):
    from .config import RAIZ
    from .direcionada import importar_alvos

    caminho = RAIZ / "data" / arquivo
    with conectar() as conn:
        p = conn.execute("SELECT chave FROM provedor WHERE tipo = 'mapillary'").fetchone()
        token = p and p["chave"]
        if exigir_foto and not token:
            raise ValueError("para escolher só alvos com foto, configure o Client Token do Mapillary")
        r = importar_alvos(conn, caminho, origem, quantidade, semente, localidade, matriculas,
                           token if exigir_foto else None, raio_m, ctx)
    if apagar:  # cópia enviada pela interface: tem dados comerciais além do que foi usado
        caminho.unlink(missing_ok=True)
    return r


@tarefa("analisar_alvos")
def _analisar_alvos(ctx, analise_id):
    from .direcionada import executar

    with conectar() as conn:
        return executar(conn, ctx, analise_id)


@tarefa("exportar_dataset")
def _exportar_dataset(ctx, dataset_id):
    from .datasets import exportar_zip

    with conectar() as conn:
        return exportar_zip(conn, ctx, dataset_id)


@tarefa("treinar")
def _treinar(ctx, treino_id):
    from .treino import treinar

    return treinar(ctx, treino_id)
