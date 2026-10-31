"""Linha de comando do GeoFAT: python -m geofat <comando> --help"""
import argparse
from pathlib import Path

from .db import conectar, migrar


def main():
    p = argparse.ArgumentParser(prog="geofat")
    sub = p.add_subparsers(dest="comando", required=True)

    c = sub.add_parser("importar-fotos", help="registra fotos georreferenciadas de data/fotos")
    c.add_argument("pasta", nargs="?", type=Path, help="subpasta dentro de data/fotos (padrão: todas)")
    c.add_argument("--fonte", choices=["existente", "captura_propria"], default="existente")

    c = sub.add_parser("detectar", help="marca fachadas nas fotos novas (modelo da etapa, página Modelos)")
    c.add_argument("--yolo", metavar="PESOS.pt", help="usar estes pesos YOLO em vez do modelo da etapa")
    c.add_argument("--limite", type=int)

    c = sub.add_parser("classificar", help="classifica o uso das fachadas (modelo da etapa, padrão Bonsai)")
    c.add_argument("--limite", type=int)

    c = sub.add_parser("importar-edificacoes", help="polígonos de edificações na área das fotos")
    c.add_argument("fonte", choices=["osm", "open-buildings"])
    c.add_argument("--arquivo", type=Path, help="CSV(.gz) do Open Buildings já baixado (padrão: baixa a célula S2)")
    c.add_argument("--confianca-min", type=float, default=0.75, help="score mínimo do Open Buildings")

    sub.add_parser("localizar", help="trajetórias, edificações candidatas e grupos sugeridos")

    c = sub.add_parser("importar-cnefe", help="importa CSV do CNEFE (IBGE)")
    c.add_argument("arquivo", type=Path)
    c.add_argument("--encoding", default="utf-8-sig")

    sub.add_parser("importar-osm", help="pontos de uso do OpenStreetMap (lojas, escolas...) como evidência")

    c = sub.add_parser("importar-gsan", help="importa CSV exportado do GSAN")
    c.add_argument("arquivo", type=Path)
    c.add_argument("--encoding", default="utf-8-sig")

    c = sub.add_parser("exportar-yolo", help="gera dataset YOLO com as fachadas revisadas")
    c.add_argument("saida", type=Path)

    c = sub.add_parser("relatorio", help="CSV dos imóveis com divergência para a equipe comercial")
    c.add_argument("saida", type=Path)

    sub.add_parser("migrar", help="atualiza o banco (tabelas novas), preservando os dados")

    a = p.parse_args()
    with conectar() as conn:
        migrar(conn)
        if a.comando == "migrar":
            print("Banco atualizado.")
        elif a.comando == "importar-fotos":
            from .config import FOTOS_DIR
            from .fotos import importar_fotos

            novas, sem_gps, erros = importar_fotos(conn, a.fonte, FOTOS_DIR / a.pasta if a.pasta else None)
            print(f"{novas} foto(s) nova(s).")
            for x in sem_gps:
                print(f"sem GPS no EXIF, ignorada: {x}")
            for x in erros:
                print(f"erro: {x}")
        elif a.comando == "detectar":
            from .detectar import carregar_yolo, detectar
            from .provedores import modelo_da_etapa, obter_detector

            if a.yolo:
                detector, nome = carregar_yolo(a.yolo), f"yolo:{Path(a.yolo).name}"
            else:
                m = modelo_da_etapa(conn, "deteccao")
                detector, nome = obter_detector(m), m["nome"]
            fotos, fachadas = detectar(conn, detector, nome, a.limite)
            print(f"{fotos} foto(s) processada(s), {fachadas} imóvel(is) criado(s).")
        elif a.comando == "classificar":
            from .classificar import classificar
            from .provedores import modelo_da_etapa, obter_classificador, obter_ocr

            mo = modelo_da_etapa(conn, "ocr", obrigatorio=False)
            perguntar, nome = obter_classificador(modelo_da_etapa(conn, "classificacao"))
            ok, falhas = classificar(conn, a.limite, perguntar, nome, ler_texto=obter_ocr(mo) if mo else None)
            print(f"{ok} imóvel(is) classificado(s).")
            for x in falhas:
                print(f"falhou (fica pendente): {x}")
        elif a.comando == "importar-edificacoes":
            from .edificacoes import importar_open_buildings, importar_osm

            n = importar_osm(conn) if a.fonte == "osm" else importar_open_buildings(conn, a.arquivo, a.confianca_min)
            print(f"{n} edificação(ões) importada(s). Rode 'localizar' para atualizar as fachadas.")
        elif a.comando == "localizar":
            from .localizar import localizar

            c = localizar(conn)
            print(f"{c['direcao']} fachada(s) localizada(s) pela direção da câmera (confiança média); "
                  f"{c['lado_via']} pelo lado da via (baixa); {c['camera']} sem edificação na linha de visada "
                  "(na câmera, posicionar no mapa).")
        elif a.comando == "importar-cnefe":
            from .publicos import importar_cnefe

            n, ignorados = importar_cnefe(conn, a.arquivo, a.encoding)
            print(f"{n} endereço(s) do CNEFE, {ignorados} sem coordenada.")
        elif a.comando == "importar-osm":
            from .publicos import importar_osm

            print(f"{importar_osm(conn)} elemento(s) do OpenStreetMap.")
        elif a.comando == "importar-gsan":
            from .gsan import importar_gsan

            n, ignorados = importar_gsan(conn, a.arquivo, a.encoding)
            print(f"{n} ligação(ões) do GSAN importada(s).")
            if ignorados:
                print(f"{len(ignorados)} linha(s) ignorada(s) (sem coordenada ou valor inválido): {ignorados[:20]}")
        elif a.comando == "exportar-yolo":
            from .yolo_dataset import exportar_yolo

            n = exportar_yolo(conn, a.saida)
            print(f"{n} foto(s) exportada(s). Treinar: yolo detect train data={a.saida / 'data.yaml'} "
                  "model=yolo11n.pt epochs=50 imgsz=640")
        elif a.comando == "relatorio":
            from .gsan import escrever_relatorio

            with open(a.saida, "wb") as f:
                escrever_relatorio(conn, f)
            print(f"Relatório salvo em {a.saida}")


if __name__ == "__main__":
    main()
