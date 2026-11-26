#!/usr/bin/env python3
"""Leva o resultado do fab_todas.py para o repositorio: docs, READMEs e indice.

Para cada placa gerada (estado ok):
    docs/placas/<slug>/img/*.png         imagens do README
    docs/placas/<slug>/gerbers/*.png     previa camada a camada (com PCB)
    docs/placas/<slug>/PREVIA-GERBERS.md
    docs/placas/<slug>/esquematico.pdf   (com esquema)
    README da placa (json 'readme', padrao docs/placas/<slug>/README.md):
        blocos <!-- fab:... --> reescritos; se o README nao existir, sai um
        esqueleto com os marcadores e o aviso de que falta o texto descritivo.

Para o repositorio:
    docs/fab-index.json   lista de todas as placas, lida pelo indice marcosdxt/placas
    README.md da raiz     bloco <!-- fab:placas --> (se existir) com a tabela

Placa que falhou neste build fica com os docs do build anterior e com a entrada
anterior do fab-index (marcada com 'falhou'). Placa ignorada entra no indice so
com o motivo.

Roda tambem no PR (sem --release): valida os marcadores dos READMEs sem
commitar nada.
"""

import argparse
import glob
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import atualiza_readme  # noqa: E402

INDICE = "docs/fab-index.json"
PREFERENCIA_IMAGEM = ("render_top.png", "placa-3d.png", "esquematico.png")


def esqueleto(item):
    titulo = (f"{item['codigo']} — {item['titulo']}" if item["codigo"] else item["titulo"])
    blocos = "\n\n".join(f"## {rotulo}\n\n<!-- fab:{nome} -->\n<!-- /fab:{nome} -->"
                         for nome, rotulo in (("imagens", "Imagens"),
                                              ("downloads", "Arquivos de fabricação"),
                                              ("esquema", "Esquema"),
                                              ("placa", "Placa"),
                                              ("bom", "Lista de materiais")))
    return (f"# {titulo}\n\n"
            "## Pendências conhecidas\n\n"
            "- README descritivo ainda não escrito: só os blocos gerados pelo CI.\n\n"
            f"{blocos}\n")


def copiar_docs(build, pasta):
    """Copia o que a placa gerou para docs/placas/<slug>, tirando o que sobrou."""
    for sub in ("img", "gerbers"):
        if os.path.isdir(os.path.join(pasta, sub)):
            shutil.rmtree(os.path.join(pasta, sub))
    os.makedirs(os.path.join(pasta, "img"), exist_ok=True)
    for png in glob.glob(os.path.join(build, "img", "*.png")):
        shutil.copy2(png, os.path.join(pasta, "img"))
    esquemas = glob.glob(os.path.join(build, "estavel", "*-esquema-latest.pdf"))
    destino_pdf = os.path.join(pasta, "esquematico.pdf")
    if esquemas:
        shutil.copy2(esquemas[0], destino_pdf)
    elif os.path.exists(destino_pdf):
        os.remove(destino_pdf)
    previa = os.path.join(build, "fab", "Previa")
    destino_md = os.path.join(pasta, "PREVIA-GERBERS.md")
    if os.path.isdir(previa):
        os.makedirs(os.path.join(pasta, "gerbers"), exist_ok=True)
        for png in glob.glob(os.path.join(previa, "*.png")):
            shutil.copy2(png, os.path.join(pasta, "gerbers"))
        texto = open(os.path.join(previa, "PREVIA-GERBERS.md"), encoding="utf-8").read()
        texto = re.sub(r"\]\(([A-Za-z0-9_]*\.png)\)", r"](gerbers/\1)", texto)
        open(destino_md, "w", encoding="utf-8").write(texto)
    elif os.path.exists(destino_md):
        os.remove(destino_md)


def imagem_principal(pasta):
    for nome in PREFERENCIA_IMAGEM:
        if os.path.isfile(os.path.join(pasta, "img", nome)):
            return f"{pasta}/img/{nome}"
    return ""


def bloco_placas(indice):
    linhas = ["| Foto | Código | Placa | Revisão |", "|---|---|---|---|"]
    for e in indice:
        if e.get("ignorado"):
            foto = "—"
            nome = f"{e['titulo']} (ignorada: {e['ignorado']})"
        else:
            foto = (f'<a href="{e["readme"]}"><img src="{e["imagem"]}" width="200"></a>'
                    if e.get("imagem") else "—")
            nome = f"[{e['titulo']}]({e['readme']})"
            if e.get("falhou"):
                nome += f" (último build falhou: {e['falhou']})"
        linhas.append(f"| {foto} | {e['codigo'] or '—'} | {nome} | {e['revisao'] or '—'} |")
    return "\n".join(linhas)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="build")
    ap.add_argument("--repo", default="", help="https://github.com/<dono>/<repo>")
    ap.add_argument("--release", default="", help="tag do Release deste build")
    args = ap.parse_args()

    resumo = json.load(open(os.path.join(args.out, "resumo.json"), encoding="utf-8"))
    anterior = {}
    if os.path.isfile(INDICE):
        try:
            anterior = {e["slug"]: e for e in json.load(open(INDICE, encoding="utf-8"))}
        except (ValueError, KeyError, TypeError):
            anterior = {}
    url_release = (f"{args.repo}/releases/tag/{args.release}"
                   if args.repo and args.release else "")

    indice = []
    for item in resumo:
        if item["estado"] == "pulado":
            if item["slug"] in anterior:
                indice.append(anterior[item["slug"]])
            continue
        slug = item["slug"]
        pasta = f"docs/placas/{slug}"
        entrada = {
            "slug": slug, "titulo": item["titulo"], "codigo": item["codigo"],
            "revisao": item.get("revisao", ""), "pasta": pasta, "readme": item["readme"],
            "imagem": "", "release": "", "ignorado": None, "modo": item["modo"],
            "versao": item["versao"], "raiz": item["raiz"],
        }
        if item["estado"] == "ignorado":
            entrada["ignorado"] = item["motivo"]
            indice.append(entrada)
            continue
        if item["estado"] == "falhou":
            velho = dict(anterior.get(slug, entrada))
            velho["falhou"] = item["motivo"]
            indice.append(velho)
            continue

        build = os.path.join(args.out, slug)
        meta = json.load(open(os.path.join(build, "meta.json"), encoding="utf-8"))
        os.makedirs(pasta, exist_ok=True)
        copiar_docs(build, pasta)
        readme = item["readme"]
        if not os.path.isfile(readme):
            print(f"::warning::{readme} nao existe: criado o esqueleto com os marcadores")
            os.makedirs(os.path.dirname(readme) or ".", exist_ok=True)
            open(readme, "w", encoding="utf-8").write(esqueleto(item))
        docs = os.path.relpath(pasta, os.path.dirname(readme) or ".")
        texto = open(readme, encoding="utf-8").read()
        texto = atualiza_readme.atualizar(texto, meta, args.repo, docs)
        open(readme, "w", encoding="utf-8", newline="\n").write(texto)
        entrada.update(imagem=imagem_principal(pasta), release=url_release,
                       revisao=meta.get("revisao", ""))
        indice.append(entrada)
        print(f"{slug}: docs em {pasta}, README {readme}")

    os.makedirs(os.path.dirname(INDICE), exist_ok=True)
    with open(INDICE, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(indice, fp, indent=2, ensure_ascii=False)
        fp.write("\n")
    if os.path.isfile("README.md"):
        texto = open("README.md", encoding="utf-8").read()
        novo = atualiza_readme.substituir(texto, "placas", bloco_placas(indice),
                                          obrigatorio=False)
        if novo != texto:
            open("README.md", "w", encoding="utf-8", newline="\n").write(novo)
    print(f"{INDICE}: {len(indice)} placa(s)")


if __name__ == "__main__":
    main()
