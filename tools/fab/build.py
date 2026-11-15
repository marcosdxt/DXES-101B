#!/usr/bin/env python3
"""Gera a documentacao de fabricacao e montagem da placa, sem o Altium.

Fontes:
    .PcbDoc   -> geometria: Gerber, furacao, pick and place, montagem, 3D, STEP, DXF
                 (lido pelo importador Altium do KiCad, headless)
    .SchDoc   -> BOM (designador, comentario, footprint, LCSC) e a impressao do
                 esquema (lido direto do binario por esquema.py / esquema_svg.py)
    .PrjPcb   -> ordem das folhas do esquema e variantes de montagem (componentes
                 nao montados), quando o projeto tem variante
    parametros.json -> processo de fabricacao e, se houver, a variante a gerar
    pecas.json      -> excecoes da BOM

Sai em <out>: fab/ com o pacote, img/ com as imagens do README, estavel/ com as
copias de nome fixo para o Release, os .zip e meta.json.
"""

import argparse
import csv
import datetime
import glob
import json
import os
import re
import shutil
import string
import subprocess
import sys
import zipfile
from collections import Counter, defaultdict

import pcbnew

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import esquema  # noqa: E402
import esquema_svg  # noqa: E402

AQUI = os.path.dirname(os.path.abspath(__file__))

# Camadas nao-cobre, fixas. O cobre entra depois, conforme a placa.
CAMADAS_FIXAS = [
    ("F.Mask", "gts", "Top Solder Mask"),
    ("B.Mask", "gbs", "Bottom Solder Mask"),
    ("F.Silkscreen", "gto", "Top Silkscreen"),
    ("B.Silkscreen", "gbo", "Bottom Silkscreen"),
    ("F.Paste", "gtp", "Top Paste"),
    ("B.Paste", "gbp", "Bottom Paste"),
    ("Edge.Cuts", "gm1", "Board Outline"),
]

CAMADAS = []


def montar_lista_camadas(n_cobre, nomes_altium=None):
    """Extensoes Protel: .gtl topo, .gbl fundo, .g1/.g2/... para as internas.

    Entram todas as camadas de cobre que o KiCad criou na importacao: ele numera
    as internas de In1.Cu a In<n-2>.Cu, na ordem do empilhamento, sejam camadas
    de sinal ou planos internos do Altium. O plano interno do Altium e negativo;
    aqui ele sai como Gerber positivo (cobre desenhado), igual as demais.
    """
    nomes_altium = nomes_altium or {}
    cobre = [("F.Cu", "gtl", "Top Copper")]
    for i in range(1, n_cobre - 1):
        nome = f"In{i}.Cu"
        descricao = f"Inner Copper {i}, positivo"
        if nomes_altium.get(nome):
            descricao += f" ('{nomes_altium[nome]}' no Altium)"
        cobre.append((nome, f"g{i}", descricao))
    cobre.append(("B.Cu", "gbl", "Bottom Copper"))
    CAMADAS[:] = cobre + CAMADAS_FIXAS
    return CAMADAS


def eh_cobre(nome):
    return nome.endswith(".Cu")


avisos = []


def avisar(msg):
    avisos.append(msg)
    print(f"::warning::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"AVISO: {msg}")


def mm(valor):
    return pcbnew.ToMM(valor)


def rodar(cmd, **kw):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, **kw)


def tentar(descricao, cmd):
    """Roda uma etapa que nao pode derrubar o pacote inteiro."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as erro:
        avisar(f"{descricao}: {erro}")
        return False
    if res.returncode != 0:
        fim = (res.stderr or res.stdout or "").strip().splitlines()[-3:]
        avisar(f"{descricao} falhou (codigo {res.returncode}): {' | '.join(fim)}")
        return False
    return True


# ----------------------------------------------------------------- geometria

def importar(pcbdoc, dir_kicad):
    """Le o .PcbDoc, normaliza a origem no canto inferior esquerdo e salva.

    Tudo o que o build precisa saber da placa e colhido aqui, de uma vez, e
    devolvido como dados simples. No KiCad 10 os objetos da API Python deixam
    de funcionar depois que outra placa e carregada ou salva no mesmo
    processo, entao nenhum objeto do pcbnew sai desta funcao.
    """
    board = pcbnew.PCB_IO_MGR.Load(pcbnew.PCB_IO_MGR.ALTIUM_DESIGNER, pcbdoc)

    # Gerber, furacao e pick and place precisam compartilhar a mesma origem.
    # Ancorar no canto da placa deixa tudo positivo, que e o que a casa espera.
    caixa = board.GetBoardEdgesBoundingBox()
    origem = pcbnew.VECTOR2I(caixa.GetLeft(), caixa.GetBottom())
    board.GetDesignSettings().SetAuxOrigin(origem)

    footprints = {}
    for fp in board.GetFootprints():
        ref = fp.GetReference().strip()
        if not ref:
            continue  # logotipo, furo de fixacao, marca de painel: nao e peca
        try:
            tht = any(pd.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH)
                      for pd in fp.Pads())
        except TypeError:
            tht = bool(fp.GetAttributes() & pcbnew.FP_THROUGH_HOLE)
        footprints[ref] = {
            "footprint": fp.GetFPIDAsString().split(":")[-1],
            "tecnologia": "THT" if tht else "SMD",
            "lado": "bottom" if fp.IsFlipped() else "top",
        }
    n_cobre = board.GetCopperLayerCount()
    # rotulo que cada camada interna tinha no Altium, so para a documentacao
    nomes_altium = {}
    for i in range(1, n_cobre - 1):
        try:
            nomes_altium[f"In{i}.Cu"] = str(
                board.GetLayerName(getattr(pcbnew, f"In{i}_Cu")))
        except Exception:
            pass
    info = {
        "largura_mm": round(mm(caixa.GetWidth()), 2),
        "altura_mm": round(mm(caixa.GetHeight()), 2),
        "camadas_cobre": n_cobre,
        "nomes_altium": nomes_altium,
        "nets": board.GetNetCount(),
        "footprints": footprints,
    }

    os.makedirs(dir_kicad, exist_ok=True)
    projeto = os.path.abspath(os.path.join(dir_kicad, "placa.kicad_pro"))
    destino = os.path.join(dir_kicad, "placa.kicad_pcb")
    gestor = pcbnew.GetSettingsManager()
    gestor.LoadProject(projeto, False)
    try:
        board.SetProject(gestor.GetProject(projeto))
    except Exception:
        pass
    pcbnew.SaveBoard(destino, board)
    info["zonas_preenchidas"] = preencher_zonas_vazias(destino)
    return info, destino


def preencher_zonas_vazias(kicad_pcb):
    """Preenche as zonas que chegaram do Altium sem cobre: os planos internos.

    O polygon pour do Altium traz o cobre ja calculado e o KiCad o importa
    preenchido. O plano interno (negativo) e o split plane nao trazem cobre
    nenhum, so o contorno: o KiCad cria a zona vazia e o Gerber da camada
    sairia em branco. Aqui essas zonas, e so elas, sao preenchidas pelo KiCad.

    Roda em outro processo (este mesmo arquivo, com --preencher-zonas): o
    preenchimento precisa da placa aberta por pcbnew.LoadBoard, que monta as
    regras de projeto, e no KiCad 10 nao da para abrir outra placa no mesmo
    processo. Se o processo cair, o build segue e a validacao dos Gerbers
    acusa a camada de cobre vazia.
    """
    try:
        res = subprocess.run([sys.executable, os.path.abspath(__file__),
                              "--preencher-zonas", kicad_pcb],
                             capture_output=True, text=True)
    except OSError as erro:
        avisar(f"preenchimento das zonas vazias nao rodou: {erro}")
        return []
    if res.returncode != 0:
        fim = (res.stderr or res.stdout or "").strip().splitlines()[-3:]
        avisar(f"preenchimento das zonas vazias falhou (codigo {res.returncode}): "
               f"{' | '.join(fim)}")
        return []
    zonas = []
    for linha in res.stdout.splitlines():
        if linha.startswith("ZONAS="):
            zonas = json.loads(linha[len("ZONAS="):])
    if zonas:
        avisar("cobre calculado pelo KiCad, e nao pelo Altium, nas zonas que vieram sem "
               f"preenchimento (planos internos): {', '.join(zonas)}. Conferir a previa "
               "dessas camadas contra o Altium antes de fabricar")
    return zonas


def _preencher_zonas(kicad_pcb):
    """Corpo do --preencher-zonas; ver preencher_zonas_vazias."""
    board = pcbnew.LoadBoard(kicad_pcb)
    vazias = []
    for zona in board.Zones():
        try:
            if zona.GetIsRuleArea() or zona.IsFilled():
                continue
        except Exception:
            continue
        vazias.append(zona)
    nomes = set()
    for zona in vazias:
        try:
            nomes.add(f"{board.GetLayerName(zona.GetFirstLayer())} / "
                      f"{zona.GetNetname() or 'sem net'}")
        except Exception:
            nomes.add("zona sem identificacao")
    nomes = sorted(nomes)
    if vazias:
        preenchedor = pcbnew.ZONE_FILLER(board)
        try:
            preenchedor.Fill(vazias)
        except TypeError:
            # a API nao aceitou a lista: recalcula todas as zonas da placa
            preenchedor.Fill(board.Zones())
            nomes.append("(todas as zonas da placa foram recalculadas)")
        pcbnew.SaveBoard(kicad_pcb, board)
    print("ZONAS=" + json.dumps(nomes, ensure_ascii=True))


def blocos_de_footprint(texto):
    """Posicoes (inicio, fim) de cada bloco '(footprint ...)' do .kicad_pcb."""
    blocos, pos = [], 0
    while True:
        achado = re.compile(r"^[ \t]*\(footprint\s", re.MULTILINE).search(texto, pos)
        if not achado:
            return blocos
        inicio = achado.start()
        nivel, i, em_texto = 0, inicio, False
        while i < len(texto):
            c = texto[i]
            if em_texto:
                if c == "\\":
                    i += 1
                elif c == '"':
                    em_texto = False
            elif c == '"':
                em_texto = True
            elif c == "(":
                nivel += 1
            elif c == ")":
                nivel -= 1
                if nivel == 0:
                    break
            i += 1
        blocos.append((inicio, i + 1))
        pos = i + 1


def placa_da_variante(kicad_pcb, nao_montados):
    """Copia da placa sem os componentes nao montados da variante.

    Serve para o desenho de montagem, o render, o PDF 3D e o STEP. Gerber e
    furacao continuam saindo da placa completa: o pad de um componente nao
    montado existe na placa do mesmo jeito.

    Os blocos sao cortados direto do texto do .kicad_pcb, sem a API Python.
    """
    if not nao_montados:
        return kicad_pcb  # sem variante, ou variante sem ausentes: a placa completa
    destino = os.path.join(os.path.dirname(kicad_pcb), "placa-variante.kicad_pcb")
    texto = open(kicad_pcb, encoding="utf-8").read()
    partes, pos, tirados = [], 0, set()
    for inicio, fim in blocos_de_footprint(texto):
        achado = re.search(r'\(property "Reference" "([^"]*)"', texto[inicio:fim])
        if achado and achado.group(1) in nao_montados:
            partes.append(texto[pos:inicio])
            pos = fim
            tirados.add(achado.group(1))
    partes.append(texto[pos:])
    open(destino, "w", encoding="utf-8").write("".join(partes))
    faltou = set(nao_montados) - tirados
    if faltou:
        avisar(f"variante: nao achei no PCB os nao montados {', '.join(sorted(faltou))}")
    return destino


def plotar_gerbers(kicad_pcb, dir_gerber, projeto, dir_trab):
    """Plota as camadas pelo kicad-cli.

    Nao usar PLOT_CONTROLLER aqui: pela API ele contamina o Edge.Cuts com
    geometria que nao pertence ao contorno, e o fabricante recortaria a placa
    errada. O kicad-cli sai limpo.

    Uma camada por chamada, cada uma numa pasta propria. O kicad-cli nomeia o
    arquivo pelo rotulo da camada vindo do Altium ("Top Layer", "Layer 1") e
    escolhe por conta propria a extensao Protel das camadas internas, entao
    casar arquivo e camada pela extensao so e seguro em placa de 2 camadas.
    Aqui o unico arquivo que sai e, por construcao, o da camada pedida, e ele
    recebe o nome <projeto>-<camada>.<ext> com a extensao da lista CAMADAS.
    """
    os.makedirs(dir_gerber, exist_ok=True)
    for nome, ext, _desc in CAMADAS:
        pasta = os.path.join(dir_trab, "gerber-" + nome.replace(".", "_"))
        os.makedirs(pasta, exist_ok=True)
        rodar(["kicad-cli", "pcb", "export", "gerbers", "--no-x2",
               "--use-drill-file-origin", "--subtract-soldermask", "--layers", nome,
               "-o", pasta, kicad_pcb])
        saidas = [a for a in sorted(os.listdir(pasta))
                  if not a.lower().endswith(".gbrjob")]
        if len(saidas) != 1:
            raise SystemExit(f"ERRO: a camada {nome} deveria gerar 1 Gerber e gerou "
                             f"{len(saidas)}: {', '.join(saidas) or 'nenhum'}")
        shutil.move(os.path.join(pasta, saidas[0]),
                    os.path.join(dir_gerber, f"{projeto}-{nome}.{ext}"))


def furar(kicad_pcb, dir_drill, projeto):
    """Furacao Excellon, na mesma origem dos Gerbers (--drill-origin plot)."""
    os.makedirs(dir_drill, exist_ok=True)
    rodar(["kicad-cli", "pcb", "export", "drill", "--format", "excellon",
           "--drill-origin", "plot", "--excellon-zeros-format", "decimal",
           "--excellon-separate-th", "--generate-map", "--map-format", "pdf",
           "-o", dir_drill + os.sep, kicad_pcb])
    for arquivo in os.listdir(dir_drill):
        if arquivo.startswith("placa"):
            os.rename(os.path.join(dir_drill, arquivo),
                      os.path.join(dir_drill, projeto + arquivo[len("placa"):]))


def ler_furacao(dir_drill):
    tabela = []
    for arquivo in sorted(os.listdir(dir_drill)):
        if not arquivo.lower().endswith(".drl"):
            continue
        tipo = "NPTH" if "NPTH" in arquivo.upper() else "PTH"
        diametros, atual, contagem = {}, None, Counter()
        with open(os.path.join(dir_drill, arquivo)) as fp:
            for linha in fp:
                linha = linha.strip()
                if linha.startswith("T") and "C" in linha:
                    ferramenta, _, diam = linha.partition("C")
                    try:
                        diametros[ferramenta] = float(diam)
                    except ValueError:
                        pass
                elif linha.startswith("T") and linha[1:].isdigit():
                    atual = linha
                elif linha.startswith("X") and atual:
                    contagem[atual] += 1
        for ferramenta, furos in sorted(contagem.items(), key=lambda kv: diametros.get(kv[0], 0)):
            tabela.append((diametros.get(ferramenta, 0.0), furos, tipo))
    return tabela


def conferir_furos_no_contorno(dir_drill, medidas):
    """Avisa sobre furo caindo fora do contorno: a casa vai abrir consulta."""
    fora = []
    for arquivo in sorted(os.listdir(dir_drill)):
        if not arquivo.lower().endswith(".drl"):
            continue
        for linha in open(os.path.join(dir_drill, arquivo)):
            achado = re.match(r"^X(-?[\d.]+)Y(-?[\d.]+)", linha.strip())
            if not achado:
                continue
            x, y = float(achado.group(1)), float(achado.group(2))
            if not (0 <= x <= medidas["largura_mm"] and 0 <= y <= medidas["altura_mm"]):
                fora.append((arquivo, x, y))
    if fora:
        arquivos = sorted({a for a, _x, _y in fora})
        avisar(f"{len(fora)} furos caem fora do contorno da placa ({', '.join(arquivos)}); "
               "confirmar com a casa antes de fabricar")
    return len(fora)


def medir(info, dir_drill, kicad_pcb):
    # Largura das trilhas lida do .kicad_pcb: no KiCad 10 o board.GetTracks()
    # da API Python devolve um objeto que nao da pra iterar.
    texto = open(kicad_pcb, encoding="utf-8").read()
    larguras = [float(w) for w in
                re.findall(r"\(segment\s.*?\(width ([\d.]+)\)", texto, re.DOTALL)]
    furacao = ler_furacao(dir_drill)
    return {
        "largura_mm": info["largura_mm"],
        "altura_mm": info["altura_mm"],
        "camadas_cobre": info["camadas_cobre"],
        "componentes": len(info["footprints"]),
        "trilhas": len(larguras),
        "nets": info["nets"],
        "trilha_minima_mm": round(min(larguras), 3) if larguras else None,
        "furo_minimo_mm": min((d for d, _, _ in furacao), default=None),
        "furos_total": sum(n for _, n, _ in furacao),
        "furos_pth": sum(n for _, n, t in furacao if t == "PTH"),
        "furos_npth": sum(n for _, n, t in furacao if t == "NPTH"),
        "furacao": [{"diametro_mm": d, "furos": n, "tipo": t} for d, n, t in furacao],
    }


def validar(dir_gerber, medidas):
    erros = []
    presentes = {a.rsplit(".", 1)[-1].lower() for a in os.listdir(dir_gerber)}
    for _nome, ext, descricao in CAMADAS:
        if ext not in presentes:
            erros.append(f"camada ausente: .{ext} ({descricao})")
    for arquivo in os.listdir(dir_gerber):
        if os.path.getsize(os.path.join(dir_gerber, arquivo)) == 0:
            erros.append(f"arquivo vazio: {arquivo}")
    # Camada de cobre sem nenhum desenho: e o que acontece com o plano interno
    # do Altium quando a zona nao foi preenchida. Nao pode ir para a fabrica.
    for nome, ext, descricao in CAMADAS:
        if not eh_cobre(nome):
            continue
        for arquivo in os.listdir(dir_gerber):
            if not arquivo.lower().endswith("." + ext):
                continue
            texto = open(os.path.join(dir_gerber, arquivo), errors="replace").read()
            if "D01" not in texto and "G36" not in texto:
                erros.append(f"camada de cobre sem desenho: {arquivo} ({descricao}); "
                             "plano interno nao preenchido na importacao?")
    if medidas["furos_total"] == 0:
        erros.append("nenhum furo na furacao")
    if erros:
        for erro in erros:
            print(f"ERRO: {erro}", file=sys.stderr)
        raise SystemExit(1)


# ----------------------------------------------------------------- montagem

A_DEFINIR = "a definir no pedido"


def sufixo(variante):
    """'(nome-da-variante)' nos nomes de arquivo; vazio se o projeto nao tem variante."""
    return f"({variante})" if variante else ""


def ordem(designador):
    letras = designador.rstrip(string.digits)
    return (letras, int(designador[len(letras):] or 0), designador)


def montar_bom(do_pcb, folhas, pecas, nao_montados):
    """Junta o esquema (BOM, LCSC) com o PCB (footprint, lado, tecnologia)."""
    do_esquema, origem_folha = {}, {}
    for folha in folhas:
        for comp in esquema.componentes(folha):
            ref = comp["designador"]
            if ref in do_esquema:
                if max(comp.get("partes", 1), do_esquema[ref].get("partes", 1)) <= 1:
                    avisar(f"designador {ref} repetido sem ser multiparte: "
                           f"{os.path.basename(origem_folha[ref])} e "
                           f"{os.path.basename(folha)}")
                continue
            do_esquema[ref] = comp
            origem_folha[ref] = folha

    so_no_esquema = sorted(set(do_esquema) - set(do_pcb), key=ordem)
    so_no_pcb = sorted(set(do_pcb) - set(do_esquema), key=ordem)
    if so_no_esquema:
        avisar(f"componentes no esquema e ausentes do PCB: {', '.join(so_no_esquema)}")
    if so_no_pcb:
        avisar(f"componentes no PCB e ausentes do esquema: {', '.join(so_no_pcb)}")
    desconhecidos = sorted(set(nao_montados) - set(do_esquema), key=ordem)
    if desconhecidos:
        avisar(f"a variante cita componentes que nao existem: {', '.join(desconhecidos)}")

    linhas = []
    for designador in sorted(set(do_pcb) & set(do_esquema), key=ordem):
        sch, pcb = do_esquema[designador], do_pcb[designador]
        if sch["footprint"] and sch["footprint"] != pcb["footprint"]:
            avisar(f"{designador}: footprint do esquema ({sch['footprint']}) difere do "
                   f"PCB ({pcb['footprint']}); atualize o PCB pelo esquema")
        peca = pecas.get(sch["lcsc"], {})
        montado = designador not in nao_montados
        linhas.append({
            "designador": designador,
            "comentario": sch["comentario"],
            "footprint": pcb["footprint"],
            "tecnologia": pcb["tecnologia"],
            "lado": pcb["lado"],
            "descricao": peca.get("descricao") or sch["descricao"],
            "lcsc": sch["lcsc"],
            "montado": montado,
            # padrao: montado na variante e com codigo LCSC vai para a JLCPCB
            "montar_jlcpcb": montado and bool(sch["lcsc"]) and peca.get("montar_jlcpcb", True),
        })
    return linhas


def agrupar_bom(linhas):
    """Mesmo agrupamento do OutJob do Altium: por Comment e Footprint."""
    grupos = defaultdict(list)
    for linha in linhas:
        if linha["montado"]:
            grupos[(linha["comentario"], linha["footprint"])].append(linha)
    saida = []
    for (comentario, footprint), itens in grupos.items():
        codigos = sorted({i["lcsc"] for i in itens if i["lcsc"]})
        if len(codigos) > 1:
            avisar(f"{comentario} / {footprint}: mais de um codigo LCSC no mesmo grupo "
                   f"({', '.join(codigos)})")
        saida.append({
            "comentario": comentario,
            "descricao": itens[0]["descricao"],
            "designadores": [i["designador"] for i in itens],
            "footprint": footprint,
            "quantidade": len(itens),
            "lcsc": ", ".join(codigos),
        })
    saida.sort(key=lambda g: ordem(g["designadores"][0]))
    return saida


# colunas do OutJob (Bill of Materials): Comment, Description, Designator,
# Footprint, Quantity, LCSC
COLUNAS_BOM = ["Comment", "Description", "Designator", "Footprint", "Quantity", "LCSC"]


def linha_bom(grupo):
    return [grupo["comentario"], grupo["descricao"], ", ".join(grupo["designadores"]),
            grupo["footprint"], grupo["quantidade"], grupo["lcsc"]]


def escrever_bom_csv(caminho, grupos):
    with open(caminho, "w", newline="", encoding="utf-8-sig") as fp:
        escritor = csv.writer(fp)
        escritor.writerow(COLUNAS_BOM)
        for grupo in grupos:
            escritor.writerow(linha_bom(grupo))


def escrever_bom_xlsx(caminho, grupos, titulo):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        avisar("openpyxl nao instalado: a BOM sai so em CSV")
        return False
    livro = Workbook()
    aba = livro.active
    aba.title = "BOM"
    aba.append([titulo])
    aba["A1"].font = Font(bold=True, size=12)
    aba.append([])
    aba.append(COLUNAS_BOM)
    for celula in aba[3]:
        celula.font = Font(bold=True, color="FFFFFF")
        celula.fill = PatternFill("solid", fgColor="404040")
    for grupo in grupos:
        aba.append(linha_bom(grupo))
    for letra, largura in zip("ABCDEF", (22, 60, 26, 36, 10, 14)):
        aba.column_dimensions[letra].width = largura
    for linha in aba.iter_rows(min_row=4):
        for celula in linha:
            celula.alignment = Alignment(vertical="top", wrap_text=True)
    aba.freeze_panes = "A4"
    livro.save(caminho)
    return True


def escrever_bom_jlcpcb(caminho, linhas):
    """BOM no formato da JLCPCB: Comment, Designator, Footprint, LCSC Part #."""
    grupos = defaultdict(list)
    for linha in linhas:
        if linha["montar_jlcpcb"]:
            grupos[(linha["comentario"], linha["footprint"], linha["lcsc"])].append(
                linha["designador"])
    with open(caminho, "w", newline="") as fp:
        escritor = csv.writer(fp)
        escritor.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
        for (comentario, footprint, lcsc), designadores in sorted(grupos.items()):
            escritor.writerow([comentario, ",".join(designadores), footprint, lcsc])
    return sum(len(d) for d in grupos.values())


def ler_posicoes(kicad_pcb, dir_trabalho):
    """Posicao de cada componente, na mesma origem dos Gerbers e da furacao."""
    bruto = os.path.join(dir_trabalho, "pos.csv")
    rodar(["kicad-cli", "pcb", "export", "pos", "--format", "csv", "--units", "mm",
           "--side", "both", "--use-drill-file-origin", "-o", bruto, kicad_pcb])
    posicoes = {}
    with open(bruto, newline="") as fp:
        for reg in csv.DictReader(fp):
            posicoes[reg["Ref"]] = {
                "x": float(reg["PosX"]), "y": float(reg["PosY"]),
                "rot": float(reg["Rot"]), "lado": reg["Side"].lower()}
    return posicoes


def escrever_pick_place(caminho, linhas, posicoes):
    """Pick and place com as colunas do OutJob do Altium, so os montados."""
    escritos = 0
    with open(caminho, "w", newline="", encoding="utf-8-sig") as fp:
        escritor = csv.writer(fp)
        escritor.writerow(["Designator", "Comment", "Layer", "Footprint",
                           "Center-X(mm)", "Center-Y(mm)", "Rotation", "Description"])
        for linha in linhas:
            pos = posicoes.get(linha["designador"])
            if not linha["montado"] or not pos:
                continue
            escritor.writerow([
                linha["designador"], linha["comentario"],
                "BottomLayer" if pos["lado"] == "bottom" else "TopLayer",
                linha["footprint"], f'{pos["x"]:.4f}', f'{pos["y"]:.4f}',
                f'{pos["rot"]:.2f}', linha["descricao"]])
            escritos += 1
    return escritos


def escrever_cpl_jlcpcb(caminho, linhas, posicoes):
    escritos = 0
    with open(caminho, "w", newline="") as fp:
        escritor = csv.writer(fp)
        escritor.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        for linha in linhas:
            pos = posicoes.get(linha["designador"])
            if not linha["montar_jlcpcb"] or not pos:
                continue
            escritor.writerow([linha["designador"], f'{pos["x"]:.4f}', f'{pos["y"]:.4f}',
                               pos["lado"].capitalize(), f'{pos["rot"]:.2f}'])
            escritos += 1
    return escritos


def desenho_de_montagem(kicad_var, dir_mont, dir_trab, projeto, variante, nao_montados,
                        lado, versao, medidas):
    """Desenho de montagem da variante: pads em cinza, serigrafia e contorno.

    Sai da placa ja sem os componentes nao montados, entao o que aparece no
    desenho e exatamente o que vai na placa.
    """
    letra = "F" if lado == "top" else "B"
    espelho = [] if lado == "top" else ["--mirror"]
    comuns = ["--mode-single", "--black-and-white", "--fit-page-to-board",
              "--exclude-drawing-sheet"] + espelho
    pads = os.path.join(dir_trab, f"montagem-{lado}-pads.svg")
    silk = os.path.join(dir_trab, f"montagem-{lado}-silk.svg")
    # Edge.Cuts nas duas, para as duas imagens terem a mesma caixa
    rodar(["kicad-cli", "pcb", "export", "svg", *comuns,
           "--layers", f"{letra}.Mask,Edge.Cuts", "-o", pads, kicad_var])
    rodar(["kicad-cli", "pcb", "export", "svg", *comuns,
           "--layers", f"{letra}.Silkscreen,Edge.Cuts", "-o", silk, kicad_var])

    larg, alt = medidas["largura_mm"], medidas["altura_mm"]
    escala = max(1.0, min(4.0, 180.0 / larg))
    L, A = larg * escala + 20, alt * escala + 34
    fora = ", ".join(sorted(nao_montados, key=ordem)) or "nenhum"
    nome_lado = "superior" if lado == "top" else "inferior (vista por baixo)"
    folha = os.path.join(dir_trab, f"montagem-{lado}.svg")

    def embutir(caminho, opacidade):
        # O SVG do KiCad entra como <svg> aninhado, e nao como <image>: assim o
        # desenho continua vetorial no PDF e nitido no PNG.
        texto = open(caminho, encoding="utf-8").read()
        raiz = re.search(r"<svg[^>]*>", texto)
        caixa = re.search(r'viewBox="([^"]*)"', raiz.group(0))
        corpo = texto[raiz.end():texto.rindex("</svg>")]
        return (f'<svg x="10" y="20" width="{larg * escala:.2f}" '
                f'height="{alt * escala:.2f}" viewBox="{caixa.group(1)}" '
                f'opacity="{opacidade}">{corpo}</svg>')

    estilo = "font-family:Arial,sans-serif"
    with open(folha, "w", encoding="utf-8") as fp:
        fp.write("\n".join([
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{L:.1f}mm" '
            f'height="{A:.1f}mm" viewBox="0 0 {L:.2f} {A:.2f}">',
            f'<rect width="{L:.2f}" height="{A:.2f}" fill="#ffffff"/>',
            f'<text x="10" y="9" style="{estilo};font-size:5px;font-weight:bold">'
            f'{projeto} - desenho de montagem, lado {nome_lado}</text>',
            f'<text x="10" y="15.5" style="{estilo};font-size:3.6px">'
            + (f'Variante: {variante} | Nao montados: {fora}</text>' if variante
               else 'Sem variante de montagem: todos os componentes montados</text>'),
            f'<text x="10" y="{A - 4:.2f}" style="{estilo};font-size:3px">'
            f'Pacote {versao} | escala {escala:.2f}:1 | placa {larg} x {alt} mm</text>',
            embutir(pads, 0.3), embutir(silk, 1), "</svg>", ""]))
    pdf = os.path.join(dir_mont, f"{projeto}-montagem-{lado}{sufixo(variante)}.pdf")
    png = os.path.join(dir_trab, f"montagem-{lado}.png")
    rodar(["rsvg-convert", "-f", "pdf", "-o", pdf, folha])
    rodar(["rsvg-convert", "-w", "2400", "-b", "white", "-o", png, folha])
    return pdf, png


# ------------------------------------------------------------------ esquema

def imprimir_esquema(folhas, dir_esq, dir_trab, projeto, variante, nao_montados):
    """Imprime cada folha do esquema em PDF (vetorial) e junta num arquivo so."""
    os.makedirs(dir_esq, exist_ok=True)
    externos = {os.path.basename(p): p for p in glob.glob(os.path.join(AQUI, "*.png"))}
    pdfs, primeira_png = [], None
    for n, folha in enumerate(folhas, 1):
        svg = os.path.join(dir_trab, f"esquema-{n}.svg")
        esquema_svg.imprimir(folha, svg, nao_montados, arquivos_externos=externos)
        pdf = os.path.join(dir_trab, f"esquema-{n}.pdf")
        rodar(["rsvg-convert", "-f", "pdf", "-o", pdf, svg])
        pdfs.append(pdf)
        if primeira_png is None:
            primeira_png = os.path.join(dir_trab, "esquematico.png")
            rodar(["rsvg-convert", "-w", "3600", "-b", "white", "-o", primeira_png, svg])
    destino = os.path.join(dir_esq, f"{projeto}-esquema{sufixo(variante)}.pdf")
    if len(pdfs) == 1:
        shutil.copy2(pdfs[0], destino)
    else:
        rodar(["pdfunite", *pdfs, destino])
    return destino, primeira_png


# ----------------------------------------------------------------- 3D e CAD

def renderizar(kicad_pcb, dir_img):
    os.makedirs(dir_img, exist_ok=True)
    vistas = {
        "render_top": ["--side", "top"],
        "render_bottom": ["--side", "bottom"],
        "placa-3d": ["--side", "top", "--perspective", "--rotate", "-50,0,25"],
        "placa-3d-tras": ["--side", "top", "--perspective", "--rotate", "-50,0,205"],
    }
    feitas = {}
    for nome, opcoes in vistas.items():
        destino = os.path.join(dir_img, nome + ".png")
        cmd = ["kicad-cli", "pcb", "render", "-o", destino, *opcoes, "--quality", "high",
               "--width", "2000", "--height", "1500", "--background", "transparent",
               kicad_pcb]
        if nome.startswith("render_"):
            rodar(cmd)
            feitas[nome] = destino
        elif tentar(f"render {nome}", cmd):
            feitas[nome] = destino
    return feitas


def pdf_3d(kicad_pcb, destino, renders):
    """PDF 3D interativo (U3D embutido), como a saida PDF3D do OutJob.

    O kicad-cli so tem 'pcb export 3dpdf' a partir do KiCad 10. Se a etapa
    falhar, sai um PDF com as vistas renderizadas, uma por pagina.
    """
    if tentar("PDF 3D", ["kicad-cli", "pcb", "export", "3dpdf", "--force",
                         "--subst-models", "--include-pads", "--include-silkscreen",
                         "--include-soldermask", "-o", destino, kicad_pcb]) \
            and os.path.exists(destino) and os.path.getsize(destino) > 0:
        return "3d"
    try:
        from PIL import Image
        paginas = []
        for caminho in renders.values():
            img = Image.open(caminho).convert("RGBA")
            fundo = Image.new("RGB", img.size, "white")
            fundo.paste(img, mask=img.split()[3])
            paginas.append(fundo)
        paginas[0].save(destino, save_all=True, append_images=paginas[1:])
        avisar("o PDF 3D saiu como vistas renderizadas, nao como modelo interativo")
        return "vistas"
    except Exception as erro:
        avisar(f"PDF 3D nao gerado: {erro}")
        return None


def exportar_step(kicad_pcb, destino):
    return tentar("STEP", ["kicad-cli", "pcb", "export", "step", "--force",
                           "--subst-models", "--drill-origin", "-o", destino,
                           kicad_pcb]) and os.path.exists(destino)


def exportar_dxf(kicad_pcb, destino):
    """DXF em mm, um arquivo so: contorno, pads/furos e serigrafia do topo."""
    return tentar("DXF", ["kicad-cli", "pcb", "export", "dxf", "--mode-single",
                          "--output-units", "mm", "--use-drill-origin",
                          "--layers", "Edge.Cuts,F.Mask,F.Silkscreen,B.Mask,B.Silkscreen",
                          "-o", destino, kicad_pcb]) and os.path.exists(destino)


def previa_camadas(kicad_pcb, dir_previa, projeto, versao):
    """Uma imagem por camada, sempre com o contorno da placa junto."""
    os.makedirs(dir_previa, exist_ok=True)
    gerados = []
    for nome, ext, descricao in CAMADAS:
        rotulo = nome.replace(".", "_")
        camadas = nome if nome == "Edge.Cuts" else f"{nome},Edge.Cuts"
        svg = os.path.join(dir_previa, rotulo + ".svg")
        ok = tentar(f"previa da camada {nome}", [
            "kicad-cli", "pcb", "export", "svg", "--mode-single", "--black-and-white",
            "--fit-page-to-board", "--exclude-drawing-sheet", "--layers", camadas,
            "-o", svg, kicad_pcb])
        if ok and tentar(f"previa da camada {nome}", [
                "rsvg-convert", "-w", "1600", "-b", "white", "-o",
                os.path.join(dir_previa, rotulo + ".png"), svg]):
            gerados.append((nome, ext, descricao, rotulo + ".png"))
        if os.path.exists(svg):
            os.remove(svg)

    linhas = [
        f"# Previa dos Gerbers — {projeto}",
        "",
        f"Versao do pacote: **{versao}**.",
        "",
        "Uma imagem por camada, renderizada do mesmo `.kicad_pcb` que gerou os",
        "Gerbers. O contorno da placa esta presente em todas, para servir de",
        "registro: qualquer camada deslocada salta aos olhos na comparacao.",
        "",
        "Conferir antes de mandar fabricar: contorno fechado, ausencia de cobre",
        "invadindo a borda, silk fora de pad, e furacao dentro do contorno.",
        "",
    ]
    for nome, ext, descricao, arquivo in gerados:
        linhas += [f"## {descricao} (`.{ext}`)", "",
                   f"Camada `{nome}` sobre o contorno da placa.", "",
                   f"![{descricao}]({arquivo})", ""]
    open(os.path.join(dir_previa, "PREVIA-GERBERS.md"), "w").write("\n".join(linhas))


# ------------------------------------------------------------------ relatos

def valor_ohms(texto):
    """'39K' -> 39000.0, '470R' -> 470.0, '2K2' -> 2200.0. None se nao for resistor."""
    achado = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*([RKM]?)(\d*)", texto.strip().upper())
    if not achado:
        return None
    inteiro, mult, resto = achado.groups()
    numero = float((inteiro + "." + resto) if resto else inteiro.replace(",", "."))
    return numero * {"": 1, "R": 1, "K": 1e3, "M": 1e6}[mult]


def dados_do_teste(params, linhas, posicoes, nao_montados):
    """O que o roteiro de teste do README tira do projeto a cada build."""
    por_ref = {l["designador"]: l for l in linhas}
    teste = params.get("teste", {})
    saida = {"pontos": [], "vout": None}

    div = teste.get("divisor_saida")
    if div:
        cima, baixo = (por_ref.get(div["r_cima"]), por_ref.get(div["r_baixo"]))
        r1 = valor_ohms(cima["comentario"]) if cima else None
        r2 = valor_ohms(baixo["comentario"]) if baixo else None
        if r1 and r2:
            vout = div["vref"] * (1 + r1 / r2)
            saida["vout"] = {"valor": round(vout, 2), "vref": div["vref"],
                             "r_cima": div["r_cima"], "r_cima_valor": cima["comentario"],
                             "r_baixo": div["r_baixo"], "r_baixo_valor": baixo["comentario"]}
        else:
            avisar("roteiro de teste: nao foi possivel ler o divisor de saida "
                   f"({div['r_cima']}/{div['r_baixo']}) do esquema")

    for ponto in teste.get("pontos", []):
        ref = ponto["componente"]
        pos = posicoes.get(ref)
        if not pos or ref not in por_ref:
            avisar(f"roteiro de teste: o ponto '{ponto['sinal']}' cita {ref}, "
                   "que nao existe na placa")
            continue
        if ref in nao_montados:
            avisar(f"roteiro de teste: o ponto '{ponto['sinal']}' fica em {ref}, "
                   "que nao e montado nesta variante")
        saida["pontos"].append({
            "sinal": ponto["sinal"], "onde": ponto["onde"], "componente": ref,
            "x": round(pos["x"], 1), "y": round(pos["y"], 1), "lado": pos["lado"]})
    return saida


def escrever_instrucoes(caminho, params, medidas, montagem, versao, nomes, variante,
                        nao_montados):
    proc, mont = params.get("processo", {}), params.get("montagem", {})
    fora = ", ".join(sorted(nao_montados, key=ordem)) or "nenhum"

    def processo(chave, unidade="", prefixo=""):
        # item de processo que o projeto nao define fica para o pedido
        valor = proc.get(chave)
        if valor in (None, "", A_DEFINIR):
            return A_DEFINIR
        return f"{prefixo}{valor}{unidade}"

    identificacao = []
    if params.get("numero_dextro"):
        identificacao.append(f"Desenho Dextro **{params['numero_dextro']}**")
    if params.get("revisao"):
        identificacao.append(f"revisao **{params['revisao']}**")
    linhas = [
        f"# Instrucoes de fabricacao e montagem — {params['projeto']}",
        "",
    ]
    if identificacao:
        frase = ", ".join(identificacao) + "."
        linhas.append(frase[0].upper() + frase[1:])
    linhas += [
        f"Versao do pacote: **{versao}**. Gerado automaticamente dos arquivos de projeto.",
        "",
        "## Fabricacao da placa",
        "",
        "| Item | Valor |",
        "| --- | --- |",
        f"| Dimensoes | {medidas['largura_mm']} x {medidas['altura_mm']} mm |",
        f"| Camadas de cobre | {medidas['camadas_cobre']} |",
        f"| Material | {processo('material')} |",
        f"| Espessura | {processo('espessura_mm', ' mm')} |",
        f"| Cobre externo | {processo('cobre_externo_oz', ' oz')} |",
    ]
    if medidas["camadas_cobre"] > 2:
        linhas.append(f"| Cobre interno | {processo('cobre_interno_oz', ' oz')} |")
        linhas.append(f"| Empilhamento | {processo('empilhamento')} |")
    linhas += [
        f"| Mascara de solda | {processo('mascara_cor')} |",
        f"| Silkscreen | {processo('silk_cor')} |",
        f"| Acabamento | {processo('acabamento')} |",
        f"| Tolerancia de contorno | {processo('tolerancia_contorno_mm', ' mm', '+/- ')} |",
        f"| Teste eletrico | {processo('teste_eletrico')} |",
        f"| Quantidade | {processo('quantidade_prototipo')} |",
        f"| Trilha minima | {medidas['trilha_minima_mm']} mm |",
        f"| Furo minimo | {medidas['furo_minimo_mm']} mm |",
        "",
        "Origem dos Gerber, da furacao e do pick and place: canto inferior esquerdo",
        "da placa, todas as coordenadas positivas e no mesmo referencial.",
        "",
        "## Montagem",
        "",
        (f"Variante de montagem: **{variante}**. Nao montados: **{fora}**." if variante
         else "O projeto nao tem variante de montagem: todos os componentes sao montados."),
        f"Casa de montagem: **{mont.get('casa') or A_DEFINIR}**. "
        f"Componentes montados no lado superior: {montagem['lado_top']}; "
        f"no lado inferior: {montagem['lado_bottom']}.",
        "",
        f"- Componentes montados: **{montagem['montados']}** de {montagem['total']}",
        f"- Com codigo LCSC (BOM e CPL no formato da JLCPCB): {montagem['jlcpcb']} "
        f"componentes, {montagem['refs_jlcpcb']} referencias",
        f"- Sem codigo LCSC (fora da BOM e do CPL da JLCPCB): "
        f"{montagem['sem_lcsc'] or 'nenhum'}",
        "",
        "## Arquivos",
        "",
        "| Arquivo | Conteudo |",
        "| --- | --- |",
    ]
    for _nome, ext, descricao in CAMADAS:
        linhas.append(f"| `Gerber/*.{ext}` | {descricao} |")
    linhas += [
        "| `NCDrill/*-PTH.drl` | Furacao metalizada (Excellon, metrico) |",
        "| `NCDrill/*-NPTH.drl` | Furacao nao metalizada (Excellon, metrico) |",
        f"| `BOM/{nomes['bom_xlsx']}` | BOM dos componentes montados, colunas do OutJob |",
        f"| `Pick Place/{nomes['pick_place']}` | Pick and place dos componentes montados, "
        "lados superior e inferior |",
        f"| `Montagem/` | Desenho de montagem de cada lado com componentes, BOM e CPL no "
        "formato JLCPCB |",
        f"| `Esquema/` | Esquema eletrico, todas as folhas; nao montados saem riscados |",
        f"| `3D/` | PDF 3D, STEP e DXF |",
        f"| `Previa/` | Uma imagem por camada, para conferencia |",
        "",
        "Gerber RS-274X, aberturas embutidas, unidade metrica, extensoes Protel.",
        "",
        "## Furacao",
        "",
        "| Diametro | Furos | Tipo |",
        "| --- | --- | --- |",
    ]
    for furo in medidas["furacao"]:
        linhas.append(f"| {furo['diametro_mm']:.2f} mm | {furo['furos']} | {furo['tipo']} |")
    linhas += [
        f"| **Total** | **{medidas['furos_total']}** | "
        f"{medidas['furos_pth']} PTH + {medidas['furos_npth']} NPTH |",
        "",
        "## Observacoes",
        "",
    ]
    linhas += [f"- {obs}" for obs in params.get("observacoes", [])] or ["- Nenhuma."]
    if avisos:
        linhas += ["", "## Avisos do build", ""] + [f"- {a}" for a in avisos]
    linhas.append("")
    open(caminho, "w").write("\n".join(linhas))


def zipar(origens, destino):
    """origens: [(pasta, prefixo dentro do zip)]."""
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as zf:
        for pasta, prefixo in origens:
            for raiz, _dirs, arquivos in os.walk(pasta):
                for arquivo in sorted(arquivos):
                    caminho = os.path.join(raiz, arquivo)
                    zf.write(caminho, os.path.join(prefixo, os.path.relpath(caminho, pasta)))


# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--params", default="tools/fab/parametros.json")
    ap.add_argument("--pecas", default="tools/fab/pecas.json")
    ap.add_argument("--out", default="build")
    ap.add_argument("--commit", default=os.environ.get("GITHUB_SHA", "local")[:7])
    ap.add_argument("--sem-3d", action="store_true",
                    help="pula render, PDF 3D e STEP (as etapas lentas)")
    ap.add_argument("--preencher-zonas", metavar="KICAD_PCB",
                    help="uso interno: preenche as zonas vazias da placa e sai")
    args = ap.parse_args()
    if args.preencher_zonas:
        _preencher_zonas(args.preencher_zonas)
        return

    params = json.load(open(args.params, encoding="utf-8"))
    pecas = {k: v for k, v in json.load(open(args.pecas, encoding="utf-8")).items()
             if not k.startswith("_")}
    projeto = params["projeto"]
    # A variante e opcional: projeto sem variante de montagem monta tudo.
    variante = params.get("variante") or ""

    todas = esquema.variantes(params["prjpcb"])
    if variante:
        if variante not in todas:
            raise SystemExit(f"ERRO: variante '{variante}' nao existe em "
                             f"{params['prjpcb']}; ha: {', '.join(todas) or 'nenhuma'}")
        nao_montados = todas[variante]
    else:
        nao_montados = set()
        if todas:
            avisar(f"{params['prjpcb']} tem variantes ({', '.join(todas)}) mas "
                   "parametros.json nao escolhe nenhuma: pacote gerado com tudo montado")

    data = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    # sem revisao definida no projeto, a versao e so data e commit
    versao = (f"rev{params['revisao']}-" if params.get("revisao") else "") \
        + f"{data}-{args.commit}"
    suf = sufixo(variante)
    nomes = {
        "zip": f"{projeto}-fab-{versao}.zip",
        "zip_gerber": f"{projeto}-gerber-drill-{versao}.zip",
        "bom_xlsx": f"Bill of Materials-{projeto}{suf}.xlsx",
        "bom_csv": f"Bill of Materials-{projeto}{suf}.csv",
        "pick_place": f"Pick Place for {projeto}{suf}.csv",
        "bom_jlcpcb": f"{projeto}-jlcpcb-bom{suf}.csv",
        "cpl_jlcpcb": f"{projeto}-jlcpcb-cpl{suf}.csv",
        "pdf3d": f"{projeto}-3D{suf}.pdf",
        "step": f"{projeto}{suf}.step",
        "dxf": f"{projeto}.dxf",
    }

    if os.path.isdir(args.out):
        shutil.rmtree(args.out)
    dir_fab = os.path.join(args.out, "fab")
    dir_trab = os.path.join(args.out, "trabalho")
    dir_img = os.path.join(args.out, "img")
    pasta = {n: os.path.join(dir_fab, n) for n in
             ("Gerber", "NCDrill", "BOM", "Pick Place", "Montagem", "Esquema", "3D", "Previa")}
    for caminho in [dir_trab, dir_img, *pasta.values()]:
        os.makedirs(caminho, exist_ok=True)

    print(f"-> importando {params['pcbdoc']}")
    info, kicad_pcb = importar(params["pcbdoc"], os.path.join(args.out, "kicad"))
    montar_lista_camadas(info["camadas_cobre"], info["nomes_altium"])
    print("   cobre: " + ", ".join(n for n, _e, _d in CAMADAS if eh_cobre(n)))
    kicad_var = placa_da_variante(kicad_pcb, nao_montados)
    if variante:
        print(f"   variante {variante}: nao montados {sorted(nao_montados, key=ordem)}")
    else:
        print("   sem variante de montagem: todos os componentes montados")

    print("-> plotando Gerbers e furacao")
    plotar_gerbers(kicad_pcb, pasta["Gerber"], projeto, dir_trab)
    furar(kicad_pcb, pasta["NCDrill"], projeto)
    medidas = medir(info, pasta["NCDrill"], kicad_pcb)
    print(f"   {medidas['largura_mm']} x {medidas['altura_mm']} mm, "
          f"{medidas['camadas_cobre']} camadas, {medidas['furos_total']} furos")
    validar(pasta["Gerber"], medidas)
    furos_fora = conferir_furos_no_contorno(pasta["NCDrill"], medidas)

    # Folhas na ordem do projeto (.PrjPcb), que e a ordem de impressao do
    # Altium: a folha de topo primeiro. O glob so entra se o .PrjPcb nao listar
    # nenhuma folha; folha no disco que o projeto nao usa fica de fora da BOM.
    no_disco = sorted(glob.glob(params.get("esquema_glob", "*.SchDoc")))
    folhas = []
    for folha in esquema.folhas_do_projeto(params["prjpcb"]):
        if os.path.exists(folha):
            folhas.append(folha)
        else:
            avisar(f"{params['prjpcb']} lista a folha {folha}, que nao esta no repositorio")
    if folhas:
        usadas = {os.path.normcase(os.path.abspath(f)) for f in folhas}
        sobrando = [f for f in no_disco
                    if os.path.normcase(os.path.abspath(f)) not in usadas]
        if sobrando:
            avisar("folhas de esquema fora do projeto, ignoradas: "
                   + ", ".join(os.path.basename(f) for f in sobrando))
    else:
        folhas = no_disco
    if not folhas:
        raise SystemExit("ERRO: nenhuma folha de esquema encontrada")
    print(f"-> BOM e pick and place a partir de {len(folhas)} folha(s) de esquema")
    linhas = montar_bom(info["footprints"], folhas, pecas, nao_montados)
    grupos = agrupar_bom(linhas)
    escrever_bom_csv(os.path.join(pasta["BOM"], nomes["bom_csv"]), grupos)
    tem_xlsx = escrever_bom_xlsx(os.path.join(pasta["BOM"], nomes["bom_xlsx"]), grupos,
                                 f"{projeto} - variante {variante} - {versao}" if variante
                                 else f"{projeto} - {versao}")
    posicoes = ler_posicoes(kicad_pcb, dir_trab)
    montados = [l for l in linhas if l["montado"]]
    no_pp = escrever_pick_place(os.path.join(pasta["Pick Place"], nomes["pick_place"]),
                                linhas, posicoes)
    if no_pp != len(montados):
        faltam = sorted({l["designador"] for l in montados} - set(posicoes), key=ordem)
        raise SystemExit(f"ERRO: {len(montados)} componentes montados mas o pick and "
                         f"place tem {no_pp}; sem posicao: {', '.join(faltam)}")
    na_jlc = escrever_bom_jlcpcb(os.path.join(pasta["Montagem"], nomes["bom_jlcpcb"]), linhas)
    no_cpl = escrever_cpl_jlcpcb(os.path.join(pasta["Montagem"], nomes["cpl_jlcpcb"]),
                                 linhas, posicoes)
    if na_jlc != no_cpl:
        raise SystemExit(f"ERRO: BOM da JLCPCB tem {na_jlc} componentes mas o CPL "
                         f"tem {no_cpl}; os dois precisam casar")
    print(f"   {len(linhas)} componentes, {len(montados)} montados, "
          f"{len(grupos)} linhas de BOM")

    montagem = {
        "total": len(linhas),
        "montados": len(montados),
        "nao_montados": sorted(nao_montados, key=ordem),
        "jlcpcb": na_jlc,
        "refs_jlcpcb": len({l["lcsc"] for l in linhas if l["montar_jlcpcb"]}),
        "sem_lcsc": ", ".join(l["designador"] for l in montados if not l["lcsc"]),
        "smd": sum(1 for l in montados if l["tecnologia"] == "SMD"),
        "tht": sum(1 for l in montados if l["tecnologia"] == "THT"),
        "lado_top": sum(1 for l in montados if l["lado"] != "bottom"),
        "lado_bottom": sum(1 for l in montados if l["lado"] == "bottom"),
    }

    print("-> desenho de montagem")
    lados = ["top"] + (["bottom"] if montagem["lado_bottom"] else [])
    for lado in lados:
        _pdf, png = desenho_de_montagem(kicad_var, pasta["Montagem"], dir_trab, projeto,
                                        variante, nao_montados, lado, versao, medidas)
        shutil.copy2(png, os.path.join(dir_img, f"montagem-{lado}.png"))

    print("-> imprimindo o esquema")
    pdf_esquema, png_esquema = imprimir_esquema(folhas, pasta["Esquema"], dir_trab,
                                                projeto, variante, nao_montados)
    shutil.copy2(png_esquema, os.path.join(dir_img, "esquematico.png"))

    print("-> previa camada a camada")
    previa_camadas(kicad_pcb, pasta["Previa"], projeto, versao)

    print("-> DXF")
    tem_dxf = exportar_dxf(kicad_pcb, os.path.join(pasta["3D"], nomes["dxf"]))
    tem_step, tipo_pdf3d = False, None
    if not args.sem_3d:
        print("-> renderizando as imagens")
        renders = renderizar(kicad_var, dir_img)
        print("-> PDF 3D")
        tipo_pdf3d = pdf_3d(kicad_var, os.path.join(pasta["3D"], nomes["pdf3d"]), renders)
        print("-> STEP")
        tem_step = exportar_step(kicad_var, os.path.join(pasta["3D"], nomes["step"]))

    teste = dados_do_teste(params, linhas, posicoes, nao_montados)
    escrever_instrucoes(os.path.join(dir_fab, "INSTRUCOES-FABRICACAO.md"), params, medidas,
                        montagem, versao, nomes, variante, nao_montados)

    pacote = os.path.join(args.out, nomes["zip"])
    zipar([(dir_fab, "")], pacote)
    pacote_gerber = os.path.join(args.out, nomes["zip_gerber"])
    zipar([(pasta["Gerber"], "Gerber"), (pasta["NCDrill"], "NCDrill")], pacote_gerber)
    print(f"-> {pacote}\n-> {pacote_gerber}")

    # Copias de nome estavel: sao elas que sustentam os links
    # /releases/latest/download/... do README, que nao podem mudar a cada versao.
    dir_estavel = os.path.join(args.out, "estavel")
    os.makedirs(dir_estavel, exist_ok=True)
    estaveis = {
        "fab": (f"{projeto}-fab-latest.zip", pacote),
        "gerber": (f"{projeto}-gerber-drill-latest.zip", pacote_gerber),
        "bom_csv": (f"{projeto}-bom-latest.csv", os.path.join(pasta["BOM"], nomes["bom_csv"])),
        "pick_place": (f"{projeto}-pick-place-latest.csv",
                       os.path.join(pasta["Pick Place"], nomes["pick_place"])),
        "bom_jlcpcb": (f"{projeto}-jlcpcb-bom-latest.csv",
                       os.path.join(pasta["Montagem"], nomes["bom_jlcpcb"])),
        "cpl_jlcpcb": (f"{projeto}-jlcpcb-cpl-latest.csv",
                       os.path.join(pasta["Montagem"], nomes["cpl_jlcpcb"])),
        "esquema": (f"{projeto}-esquema-latest.pdf", pdf_esquema),
        "montagem": (f"{projeto}-montagem-latest.pdf",
                     os.path.join(pasta["Montagem"], f"{projeto}-montagem-top{suf}.pdf")),
    }
    if "bottom" in lados:
        estaveis["montagem_bottom"] = (
            f"{projeto}-montagem-bottom-latest.pdf",
            os.path.join(pasta["Montagem"], f"{projeto}-montagem-bottom{suf}.pdf"))
    if tem_xlsx:
        estaveis["bom_xlsx"] = (f"{projeto}-bom-latest.xlsx",
                                os.path.join(pasta["BOM"], nomes["bom_xlsx"]))
    if tipo_pdf3d:
        estaveis["pdf3d"] = (f"{projeto}-3D-latest.pdf", os.path.join(pasta["3D"], nomes["pdf3d"]))
    if tem_step:
        estaveis["step"] = (f"{projeto}-latest.step", os.path.join(pasta["3D"], nomes["step"]))
    if tem_dxf:
        estaveis["dxf"] = (f"{projeto}-latest.dxf", os.path.join(pasta["3D"], nomes["dxf"]))
    for estavel, origem_arq in estaveis.values():
        shutil.copy2(origem_arq, os.path.join(dir_estavel, estavel))

    medidas.update({
        "furos_fora_do_contorno": furos_fora, "projeto": projeto, "versao": versao,
        "commit": args.commit, "revisao": params.get("revisao") or "", "variante": variante,
        "camadas": [{"camada": n, "ext": e, "descricao": d} for n, e, d in CAMADAS],
        "zonas_preenchidas": info["zonas_preenchidas"],
        "variantes": {n: sorted(f, key=ordem) for n, f in todas.items()},
        "montagem": montagem, "bom": grupos, "teste": teste, "nomes": nomes,
        "estaveis": {k: v[0] for k, v in estaveis.items()},
        "pdf3d": tipo_pdf3d, "imagens": sorted(os.listdir(dir_img)),
        "avisos": avisos,
        "casa_montagem": params.get("montagem", {}).get("casa") or A_DEFINIR,
        "lados_montagem": lados,
        "folhas": [os.path.basename(f) for f in folhas],
    })
    json.dump(medidas, open(os.path.join(args.out, "meta.json"), "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
