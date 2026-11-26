#!/usr/bin/env python3
"""Gera o pacote de fabricacao de todas as placas do repositorio.

Cada placa e um json em tools/fab/placas/<slug>.json (chaves em LEIAME do kit).
Cada uma roda num processo proprio do build.py, com tempo limite: no KiCad 10
nao da para abrir duas placas no mesmo processo, e uma placa que trava ou
quebra nao pode levar as outras junto.

Sai em <out>/<slug>/ (o pacote de cada placa) e em <out>/resumo.json:
    [{slug, titulo, codigo, raiz, readme, estado: ok|falhou|ignorado,
      motivo, duracao_s, versao, modo}]

O codigo de saida e sempre 0 quando o resumo foi escrito: quem decide se o job
falha e o passo final do workflow (--conferir), depois de publicar as placas
que deram certo.
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import time

AQUI = os.path.dirname(os.path.abspath(__file__))


def ler_placas(pasta):
    placas = []
    for caminho in sorted(glob.glob(os.path.join(pasta, "*.json"))):
        with open(caminho, encoding="utf-8") as fp:
            params = json.load(fp)
        slug = params.get("slug") or os.path.splitext(os.path.basename(caminho))[0]
        placas.append((caminho, slug, params))
    slugs = [s for _c, s, _p in placas]
    repetidos = sorted({s for s in slugs if slugs.count(s) > 1})
    if repetidos:
        raise SystemExit(f"ERRO: slug repetido em {pasta}: {', '.join(repetidos)}")
    return placas


def readme_da_placa(slug, params):
    return params.get("readme") or f"docs/placas/{slug}/README.md"


def gerar(args):
    placas = ler_placas(args.placas)
    if not placas:
        raise SystemExit(f"ERRO: nenhuma placa em {args.placas}/*.json")
    os.makedirs(args.out, exist_ok=True)
    resumo = []
    for caminho, slug, params in placas:
        item = {
            "slug": slug, "titulo": params.get("titulo") or params.get("projeto") or slug,
            "codigo": params.get("codigo") or "", "raiz": params.get("raiz") or ".",
            "readme": readme_da_placa(slug, params), "json": caminho,
            "revisao": params.get("revisao") or "",
            "estado": "", "motivo": "", "duracao_s": 0, "versao": "", "modo": "",
        }
        resumo.append(item)
        if args.so and slug not in args.so:
            item.update(estado="pulado", motivo="fora do --so")
            continue
        if params.get("ignorar"):
            item.update(estado="ignorado", motivo=str(params["ignorar"]))
            print(f"== {slug}: ignorada ({params['ignorar']})")
            continue
        destino = os.path.abspath(os.path.join(args.out, slug))
        cmd = [sys.executable, os.path.join(AQUI, "build.py"), "--placa",
               os.path.abspath(caminho), "--out", destino, "--commit", args.commit]
        if args.sem_3d:
            cmd.append("--sem-3d")
        print(f"::group::placa {slug}" if os.environ.get("GITHUB_ACTIONS")
              else f"== placa {slug}", flush=True)
        inicio = time.monotonic()
        try:
            # sem buffer: a saida do build.py cai dentro do grupo da placa no log
            res = subprocess.run(cmd, timeout=args.timeout,
                                 env={**os.environ, "PYTHONUNBUFFERED": "1"})
            codigo = res.returncode
            erro = f"build.py saiu com codigo {codigo}" if codigo else ""
        except subprocess.TimeoutExpired:
            codigo, erro = -1, f"passou do tempo limite de {args.timeout} s"
        item["duracao_s"] = round(time.monotonic() - inicio, 1)
        if os.environ.get("GITHUB_ACTIONS"):
            print("::endgroup::", flush=True)
        meta = os.path.join(destino, "meta.json")
        if codigo == 0 and os.path.isfile(meta):
            with open(meta, encoding="utf-8") as fp:
                dados = json.load(fp)
            item.update(estado="ok", versao=dados["versao"], modo=dados["modo"])
            print(f"== {slug}: ok em {item['duracao_s']} s ({dados['modo']})")
        else:
            item.update(estado="falhou", motivo=erro or "meta.json nao foi gerado")
            msg = f"placa {slug} falhou: {item['motivo']} (ver o grupo 'placa {slug}' do log)"
            print(f"::error::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"ERRO: {msg}")
    with open(os.path.join(args.out, "resumo.json"), "w", encoding="utf-8") as fp:
        json.dump(resumo, fp, indent=2, ensure_ascii=False)
    escrever_sumario(resumo)


def escrever_sumario(resumo):
    linhas = ["| Placa | Estado | Modo | Tempo | Motivo |", "|---|---|---|---|---|"]
    for i in resumo:
        linhas.append(f"| `{i['slug']}` | {i['estado']} | {i['modo'] or '—'} | "
                      f"{i['duracao_s']} s | {i['motivo'] or '—'} |")
    texto = "\n".join(linhas)
    print(texto)
    destino = os.environ.get("GITHUB_STEP_SUMMARY")
    if destino:
        with open(destino, "a", encoding="utf-8") as fp:
            fp.write("## Placas\n\n" + texto + "\n")


def conferir(args):
    """Falha se alguma placa nao marcada como 'ignorar' falhou."""
    with open(os.path.join(args.out, "resumo.json"), encoding="utf-8") as fp:
        resumo = json.load(fp)
    falhas = [i for i in resumo if i["estado"] == "falhou"]
    for i in falhas:
        print(f"ERRO: placa {i['slug']} ({i['json']}): {i['motivo']}")
    if falhas:
        print("Corrija a placa ou marque \"ignorar\": \"<motivo>\" no json dela.")
        raise SystemExit(1)
    print(f"{len(resumo)} placa(s): nenhuma falha fora das ignoradas.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--placas", default="tools/fab/placas")
    ap.add_argument("--out", default="build")
    ap.add_argument("--commit", default=os.environ.get("GITHUB_SHA", "local")[:7])
    ap.add_argument("--sem-3d", action="store_true")
    ap.add_argument("--so", nargs="*", help="gera so estes slugs (uso local)")
    ap.add_argument("--timeout", type=int,
                    default=int(os.environ.get("FAB_TIMEOUT_PLACA", "2700")),
                    help="tempo limite por placa, em segundos")
    ap.add_argument("--precisa-kicad", action="store_true",
                    help="sai com 0 se alguma placa usa o KiCad (tem .PcbDoc), senao 1")
    ap.add_argument("--conferir", action="store_true",
                    help="so le o resumo.json e falha se alguma placa falhou")
    args = ap.parse_args()
    if args.precisa_kicad:
        # codigo 0: alguma placa nao ignorada tem .PcbDoc (o build vai usar o KiCad)
        usa = any(p.get("pcbdoc") and not p.get("ignorar") and not p.get("ignorar_pcb")
                  for _c, _s, p in ler_placas(args.placas))
        raise SystemExit(0 if usa else 1)
    if args.conferir:
        conferir(args)
    else:
        gerar(args)


if __name__ == "__main__":
    main()
