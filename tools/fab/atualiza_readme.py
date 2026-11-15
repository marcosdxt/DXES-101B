#!/usr/bin/env python3
"""Reescreve os blocos gerados do README com os dados do pacote de fabricacao.

So mexe entre os marcadores <!-- fab:nome --> ... <!-- /fab:nome -->; o texto
escrito a mao fica intacto.

Marcadores obrigatorios: imagens, downloads, esquema, placa, bom.
Marcadores opcionais (so sao escritos se existirem no README): variantes, teste.
"""

import argparse
import json
import re

INICIO = "<!-- fab:{nome} -->"
FIM = "<!-- /fab:{nome} -->"


def substituir(texto, nome, conteudo, obrigatorio=True):
    inicio, fim = INICIO.format(nome=nome), FIM.format(nome=nome)
    padrao = re.compile(re.escape(inicio) + r".*?" + re.escape(fim), re.DOTALL)
    if not padrao.search(texto):
        if not obrigatorio:
            print(f"marcador opcional '{nome}' ausente do README: bloco nao escrito")
            return texto
        raise SystemExit(f"ERRO: marcador '{nome}' nao encontrado no README")
    return padrao.sub(lambda _m: f"{inicio}\n{conteudo}\n{fim}", texto)


def da_variante(meta, com, sem):
    """Trecho de frase que muda conforme o projeto tem ou nao variante de montagem."""
    return com.format(v=meta["variante"]) if meta.get("variante") else sem


def curto(texto, limite=70):
    texto = texto.replace("|", "/")
    return texto if len(texto) <= limite else texto[:limite - 1].rstrip() + "…"


def bloco_imagens(meta):
    imagens = meta.get("imagens", [])
    linhas = []
    if "placa-3d.png" in imagens:
        linhas += ["![Vista 3D da placa](docs/img/placa-3d.png)", ""]
    linhas += [
        "| Lado superior | Lado inferior |",
        "|---|---|",
        "| ![Lado superior](docs/img/render_top.png) | "
        "![Lado inferior](docs/img/render_bottom.png) |",
        "",
        "> Imagens" + da_variante(meta, " da variante `{v}`,", "") + " renderizadas pelo "
        f"CI a partir do `.PcbDoc` (pacote `{meta['versao']}`).",
    ]
    return "\n".join(linhas)


def bloco_downloads(meta, base):
    est = meta["estaveis"]

    def link(chave, rotulo):
        return f"[⬇ {rotulo}]({base}/releases/latest/download/{est[chave]})"

    itens = [
        ("fab", "Pacote completo", "Tudo abaixo num zip, com instruções de fabricação"),
        ("gerber", "Gerber + furação", "Zip para a fábrica de placas: Gerber RS-274X e NC Drill Excellon"),
        ("bom_xlsx", "BOM (xlsx)", "Colunas do OutJob: Comment, Description, Designator, Footprint, Quantity, LCSC"),
        ("bom_csv", "BOM (csv)", "A mesma BOM em CSV"),
        ("pick_place", "Pick and place", "Designator, Comment, Layer, Footprint, Center-X, Center-Y, Rotation, Description"),
        ("montagem", "Desenho de montagem", "PDF do lado superior"
         + da_variante(meta, ", sem os componentes não montados da variante", "")),
        ("montagem_bottom", "Desenho de montagem, lado inferior",
         "PDF do lado inferior, visto por baixo"),
        ("esquema", "Esquema elétrico", f"PDF vetorial, {len(meta['folhas'])} folha(s)"
         + da_variante(meta, "; os não montados saem riscados", "")),
        ("pdf3d", "PDF 3D", "Modelo 3D interativo (abrir no Adobe Reader)" if meta.get("pdf3d") == "3d"
         else "Vistas 3D renderizadas"),
        ("step", "STEP", "Modelo 3D da placa montada"),
        ("dxf", "DXF", "Contorno, pads, furos e serigrafia, em mm"),
        ("bom_jlcpcb", "BOM JLCPCB", "Comment, Designator, Footprint, LCSC Part #"),
        ("cpl_jlcpcb", "CPL JLCPCB", "Designator, Mid X, Mid Y, Layer, Rotation"),
    ]
    fora = ", ".join(meta["montagem"]["nao_montados"]) or "nenhum"
    linhas = [
        "Gerados pelo CI"
        + da_variante(meta, " para a variante **`{v}`** (não montados: " + fora + ")",
                      " (projeto sem variante de montagem: todos os componentes montados)")
        + f". Versão publicada: **`{meta['versao']}`**.",
        "",
        "| Baixar | Conteúdo |",
        "|---|---|",
    ]
    linhas += [f"| {link(chave, rotulo)} | {descricao} |"
               for chave, rotulo, descricao in itens if chave in est]
    linhas += [
        "",
        "Os links apontam sempre para o último pacote publicado. Se o repositório for "
        "privado, eles exigem sessão do GitHub no navegador; por script, use "
        "`gh release download --pattern '*-latest.*'`. A prévia camada a camada dos "
        "Gerbers está em [`docs/PREVIA-GERBERS.md`](docs/PREVIA-GERBERS.md).",
    ]
    if meta.get("avisos"):
        linhas += ["", "> **Avisos do último build:**", ">"]
        linhas += [f"> - {a}" for a in meta["avisos"]]
    return "\n".join(linhas)


def bloco_esquema(meta, base):
    folhas = meta["folhas"]
    if len(folhas) == 1:
        origem = f"direto do `{folhas[0]}`"
        legenda = "![Esquema elétrico](docs/img/esquematico.png)"
    else:
        origem = (f"direto das {len(folhas)} folhas do projeto ("
                  + ", ".join(f"`{f}`" for f in folhas) + ")")
        legenda = (f"![Esquema elétrico, folha 1 de {len(folhas)}]"
                   "(docs/img/esquematico.png)")
    linhas = [
        f"PDF" + (f" com as {len(folhas)} folhas" if len(folhas) > 1 else "")
        + ": [`docs/esquematico.pdf`](docs/esquematico.pdf)",
        "",
        legenda,
        "",
    ]
    if len(folhas) > 1:
        linhas += [f"A imagem acima é a primeira folha (`{folhas[0]}`); as demais estão "
                   "no PDF.", ""]
    linhas.append(
        f"Impresso pelo CI {origem} (pacote `{meta['versao']}`)"
        + da_variante(
            meta,
            ", na variante `{v}`: os componentes não montados ("
            + (", ".join(meta["montagem"]["nao_montados"]) or "nenhum")
            + ") saem riscados em vermelho.", "."))
    return "\n".join(linhas)


def bloco_placa(meta):
    furos = sorted(meta["furacao"], key=lambda f: f["diametro_mm"])
    faixa = (f"de {furos[0]['diametro_mm']:.2f} mm a {furos[-1]['diametro_mm']:.2f} mm"
             if furos else "")
    mont = meta["montagem"]
    imagens = meta.get("imagens", [])
    linhas = [
        "| Item | Valor |",
        "|---|---|",
        f"| Contorno | {meta['largura_mm']} × {meta['altura_mm']} mm |",
        f"| Camadas de cobre | {meta['camadas_cobre']} |",
        f"| Componentes na placa | {meta['componentes']} |",
        "| Montados" + da_variante(meta, " na variante `{v}`", "")
        + f" | {mont['montados']} ({mont['smd']} SMD + {mont['tht']} PTH) |",
    ]
    if "lado_top" in mont:
        linhas.append(f"| Montados por lado | {mont['lado_top']} no superior, "
                      f"{mont['lado_bottom']} no inferior |")
    linhas += [
        f"| Nets | {meta['nets']} |",
        f"| Trilha mínima | {meta['trilha_minima_mm']} mm |",
        f"| Furação | {meta['furos_total']} furos ({meta['furos_pth']} metalizados + "
        f"{meta['furos_npth']} não metalizados), {faixa} |",
        "",
        "![Posição dos componentes, lado superior](docs/img/montagem-top.png)",
        "",
    ]
    if "montagem-bottom.png" in imagens:
        linhas += ["![Posição dos componentes, lado inferior](docs/img/montagem-bottom.png)",
                   ""]
    linhas.append("Desenho de montagem" + da_variante(meta, " da variante `{v}`", "")
                  + ", gerado pelo CI.")
    return "\n".join(linhas)


def bloco_bom(meta, base):
    est = meta["estaveis"]
    chave = "bom_xlsx" if "bom_xlsx" in est else "bom_csv"
    linhas = [
        f"Planilha: [`{est[chave]}`]({base}/releases/latest/download/{est[chave]})"
        + da_variante(meta, " (variante `{v}`)", "") + ". Os códigos são da LCSC.",
        "",
        "| Ref. | Qtd. | Valor | Descrição | Footprint | LCSC |",
        "|---|---|---|---|---|---|",
    ]
    for g in meta["bom"]:
        lcsc = ", ".join(f"[{c}](https://www.lcsc.com/product-detail/{c}.html)"
                         for c in g["lcsc"].split(", ") if c) or "—"
        linhas.append(f"| {', '.join(g['designadores'])} | {g['quantidade']} | "
                      f"{curto(g['comentario'])} | {curto(g['descricao'])} | "
                      f"`{g['footprint']}` | {lcsc} |")
    sem = meta["montagem"].get("sem_lcsc")
    if sem:
        linhas += ["", f"Sem código LCSC: {sem}."]
    return "\n".join(linhas)


def bloco_variantes(meta):
    if not meta["variantes"]:
        return "O projeto não tem variantes de montagem: todos os componentes são montados."
    linhas = ["| Variante | Não montados | Pacote do CI |", "|---|---|---|"]
    for nome, fora in meta["variantes"].items():
        gerada = "sim: BOM, pick and place, montagem, 3D" if nome == meta["variante"] else "não"
        linhas.append(f"| `{nome}` | {', '.join(fora) or '—'} | {gerada} |")
    return "\n".join(linhas)


def bloco_teste(meta):
    teste, mont = meta.get("teste", {}), meta["montagem"]
    fora = ", ".join(mont["nao_montados"]) or "nenhum"
    linhas = [
        f"Dados da placa sob teste, tirados do projeto pelo CI (pacote `{meta['versao']}`):",
        "",
        "- " + da_variante(meta, "Variante montada: **`{v}`**", "Sem variante de montagem")
        + f", {mont['montados']} componentes ({mont['smd']} SMD + {mont['tht']} PTH).",
        f"- Posições que devem estar **vazias**: {fora}. Conferir na inspeção.",
    ]
    vout = teste.get("vout")
    if vout:
        linhas.append(
            f"- Tensão de saída esperada: **{vout['valor']:.2f} V** "
            f"= {vout['vref']} V × (1 + {vout['r_cima']}/{vout['r_baixo']}), com "
            f"{vout['r_cima']} = {vout['r_cima_valor']} e {vout['r_baixo']} = "
            f"{vout['r_baixo_valor']} lidos do esquema. Faixa de ± 3 %: "
            f"{vout['valor'] * 0.97:.2f} a {vout['valor'] * 1.03:.2f} V.")
    if teste.get("pontos"):
        linhas += [
            "",
            "Pontos de medição. X e Y em mm a partir do canto inferior esquerdo da placa, "
            "no centro do componente (mesma origem do pick and place):",
            "",
            "| Sinal | Onde medir | Componente | X | Y | Lado |",
            "|---|---|---|---|---|---|",
        ]
        for p in teste["pontos"]:
            lado = "superior" if p["lado"] in ("top", "front") else "inferior"
            linhas.append(f"| `{p['sinal']}` | {p['onde']} | {p['componente']} | "
                          f"{p['x']:.1f} | {p['y']:.1f} | {lado} |")
    return "\n".join(linhas)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta", default="build/meta.json")
    ap.add_argument("--readme", default="README.md")
    ap.add_argument("--release", default="",
                    help="URL base do repositorio, para montar os links de download")
    args = ap.parse_args()

    with open(args.meta, encoding="utf-8") as fp:
        meta = json.load(fp)
    with open(args.readme, encoding="utf-8") as fp:
        texto = fp.read()
    base = args.release.rstrip("/") or "."

    texto = substituir(texto, "imagens", bloco_imagens(meta))
    texto = substituir(texto, "downloads", bloco_downloads(meta, base))
    texto = substituir(texto, "esquema", bloco_esquema(meta, base))
    texto = substituir(texto, "placa", bloco_placa(meta))
    texto = substituir(texto, "bom", bloco_bom(meta, base))
    # opcionais: projeto sem variante ou sem roteiro de teste nao precisa dos blocos
    texto = substituir(texto, "variantes", bloco_variantes(meta), obrigatorio=False)
    texto = substituir(texto, "teste", bloco_teste(meta), obrigatorio=False)

    with open(args.readme, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(texto)
    print(f"README atualizado (pacote {meta['versao']})")


if __name__ == "__main__":
    main()
