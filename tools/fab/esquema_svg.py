#!/usr/bin/env python3
"""Imprime o .SchDoc do Altium em SVG, sem o Altium.

Desenha os registros do stream FileHeader (ver esquema.py): fios, juncoes,
portas de alimentacao, corpo e pinos dos componentes, textos e imagens, e os
objetos do esquema hierarquico: simbolo de folha com suas entradas e portas. A
unidade do arquivo e 1/100 de polegada, com o Y para cima; aqui o Y e invertido.

Com a lista de 'nao montados' de uma variante, os componentes ausentes saem
riscados com um X vermelho, como na impressao com variante do Altium.

O PDF e o PNG saem do SVG pelo rsvg-convert, no build.py.
"""

import base64
import datetime
import math
import os
import re
import struct
import sys
import zlib
from xml.sax.saxutils import escape as _escape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import esquema  # noqa: E402

# Caracteres de controle (ex.: \x16 que o Altium deixa no fim de um rotulo de
# net) nao sao permitidos em XML 1.0 e fazem o rsvg-convert recusar o SVG.
_CONTROLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def escape(texto):
    return _escape(_CONTROLE.sub("", texto))


def com_barra(texto):
    """Texto do Altium em SVG, com a barra de sinal negado.

    No Altium, 'R\\D\\' e 'RD' com barra em cima: cada caractere seguido de
    '\\' leva a barra. Os trechos com barra viram <tspan> com overline.
    """
    if "\\" not in texto:
        return escape(texto)
    trechos, i = [], 0
    while i < len(texto):
        barra = i + 1 < len(texto) and texto[i + 1] == "\\"
        if texto[i] != "\\":
            if trechos and trechos[-1][0] == barra:
                trechos[-1][1].append(texto[i])
            else:
                trechos.append((barra, [texto[i]]))
        i += 2 if barra else 1
    return "".join(
        f'<tspan style="text-decoration:overline">{escape("".join(c))}</tspan>' if b
        else escape("".join(c)) for b, c in trechos)


# SheetStyle -> (largura, altura) em 1/100 pol
FOLHAS = {0: (1150, 760), 1: (1550, 1110), 2: (2230, 1570), 3: (3150, 2230),
          4: (4460, 3150), 5: (950, 750), 6: (1500, 950), 7: (2000, 1500),
          8: (3200, 2000), 9: (4200, 3200), 10: (1100, 850), 11: (1400, 850),
          12: (1700, 1100), 13: (990, 790), 14: (1540, 990), 15: (2060, 1560),
          16: (3260, 2060), 17: (4280, 3280)}
ESPESSURAS = {0: 0.5, 1: 1.0, 2: 3.0, 3: 5.0}
# altura do em, em unidades da folha, por ponto de fonte do Altium (calibrado
# contra a impressao do proprio Altium)
EM_POR_PONTO = 0.95


def num(reg, chave, padrao=0.0):
    try:
        inteiro = float(reg.get(chave, padrao))
    except ValueError:
        return padrao
    return inteiro + float(reg.get(chave + "_Frac", 0) or 0) / 100000


def cor(reg, chave="Color", padrao="#000000"):
    valor = reg.get(chave)
    if valor is None or not valor.lstrip("-").isdigit():
        return padrao
    valor = int(valor)
    return "#%02x%02x%02x" % (valor & 255, (valor >> 8) & 255, (valor >> 16) & 255)


def bmp_para_png(dados):
    """BMP sem compressao de 24 ou 32 bits -> PNG. Outros formatos: None."""
    if dados[:2] != b"BM":
        return None
    inicio = struct.unpack_from("<I", dados, 10)[0]
    largura, altura = struct.unpack_from("<ii", dados, 18)
    bits, compressao = struct.unpack_from("<HI", dados, 28)
    if bits not in (24, 32) or compressao not in (0, 3):
        return None
    bpp = bits // 8
    passo = (largura * bpp + 3) & ~3
    de_cima = altura < 0
    altura = abs(altura)
    linhas = bytearray()
    for y in range(altura):
        fonte = y if de_cima else altura - 1 - y
        linha = dados[inicio + fonte * passo:inicio + fonte * passo + largura * bpp]
        rgb = bytearray(largura * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = linha[2::bpp], linha[1::bpp], linha[0::bpp]
        linhas += b"\x00" + rgb

    def bloco(tipo, corpo):
        return (struct.pack(">I", len(corpo)) + tipo + corpo
                + struct.pack(">I", zlib.crc32(tipo + corpo) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + bloco(b"IHDR", struct.pack(">IIBBBBB", largura, altura, 8, 2, 0, 0, 0))
            + bloco(b"IDAT", zlib.compress(bytes(linhas), 6)) + bloco(b"IEND", b""))


def uri_da_imagem(dados):
    if dados[:2] == b"BM":
        png = bmp_para_png(dados)
        if png:
            return "data:image/png;base64," + base64.b64encode(png).decode()
        return "data:image/bmp;base64," + base64.b64encode(dados).decode()
    tipo = "png" if dados[:4] == b"\x89PNG" else "jpeg"
    return f"data:image/{tipo};base64," + base64.b64encode(dados).decode()


class Desenho:
    def __init__(self, caminho, nao_montados=(), data=None, arquivos_externos=None):
        self.caminho = caminho
        self.recs = esquema.registros(caminho)
        self.nao_montados = set(nao_montados)
        self.data = data or datetime.datetime.now()
        self.externos = arquivos_externos or {}
        self.folha = next((r for r in self.recs if r.get("RECORD") == "31"), {})
        if self.folha.get("UseCustomSheet") == "T":
            self.L, self.A = num(self.folha, "CustomX"), num(self.folha, "CustomY")
        else:
            self.L, self.A = FOLHAS.get(int(self.folha.get("SheetStyle", 0) or 0),
                                        FOLHAS[0])
        self.fontes = self._fontes()
        self.params_folha = {
            r["Name"].lower(): r.get("Text", "") for r in self.recs
            if r.get("RECORD") == "41" and r.get("Name") and "OwnerIndex" not in r}
        self.saida = []
        # caixa de cada componente, para riscar os nao montados
        self.caixas = {}

    # ------------------------------------------------------------ utilidades

    def _fontes(self):
        fontes = {}
        for i in range(1, int(self.folha.get("FontIdCount", 0) or 0) + 1):
            nome = self.folha.get(f"FontName{i}", "Times New Roman")
            if not nome.isascii() or nome.lower() == "default":
                nome = "Times New Roman"
            fontes[str(i)] = {
                "nome": nome,
                "tamanho": float(self.folha.get(f"Size{i}", 10) or 10),
                "negrito": self.folha.get(f"Bold{i}") == "T",
                "italico": self.folha.get(f"Italic{i}") == "T",
                "sublinhado": self.folha.get(f"Underline{i}") == "T",
                "rotacao": int(float(self.folha.get(f"Rotation{i}", 0) or 0)),
            }
        return fontes

    def y(self, valor):
        return self.A - valor

    def ponto(self, reg, prefixo="Location"):
        return num(reg, prefixo + ".X"), self.y(num(reg, prefixo + ".Y"))

    def pontos(self, reg):
        return [(num(reg, f"X{i}"), self.y(num(reg, f"Y{i}")))
                for i in range(1, int(reg.get("LocationCount", 0) or 0) + 1)]

    def espessura(self, reg, padrao=1):
        return ESPESSURAS.get(int(reg.get("LineWidth", padrao) or 0), 1.0)

    def marcar(self, dono, *pts):
        if dono is None:
            return
        caixa = self.caixas.setdefault(dono, [1e9, 1e9, -1e9, -1e9])
        for x, y in pts:
            caixa[0], caixa[1] = min(caixa[0], x), min(caixa[1], y)
            caixa[2], caixa[3] = max(caixa[2], x), max(caixa[3], y)

    def resolver(self, texto, irmaos=None):
        """'=title' -> parametro da folha (ou do componente); '*' -> vazio."""
        if texto == "*":
            return ""
        if not texto.startswith("="):
            return texto
        chave = texto[1:].strip().lower()
        if irmaos and chave in irmaos:
            return irmaos[chave]
        especiais = {
            "currentdate": self.data.strftime("%d/%m/%Y"),
            "currenttime": self.data.strftime("%H:%M"),
            "documentname": os.path.basename(self.caminho),
            "documentfullpathandname": os.path.basename(self.caminho),
        }
        if chave in especiais:
            return especiais[chave]
        valor = self.params_folha.get(chave, "")
        return "" if valor == "*" else valor

    def texto(self, x, y, conteudo, fonte_id="1", cor_="#000000", rotacao=0,
              ancora="start", vertical="base", url=None):
        if not conteudo:
            return
        fonte = self.fontes.get(str(fonte_id)) or {
            "nome": "Times New Roman", "tamanho": 10, "negrito": False,
            "italico": False, "sublinhado": False, "rotacao": 0}
        em = fonte["tamanho"] * EM_POR_PONTO
        # o Altium ancora na base da caixa do texto, nao na linha de base
        dy = {"base": -0.27 * em, "meio": 0.32 * em, "topo": 0.80 * em}[vertical]
        estilo = [f"font-family:'{fonte['nome']}',serif", f"font-size:{em:.2f}px",
                  f"fill:{cor_}"]
        if fonte["negrito"]:
            estilo.append("font-weight:bold")
        if fonte["italico"]:
            estilo.append("font-style:italic")
        if fonte["sublinhado"]:
            estilo.append("text-decoration:underline")
        giro = f" rotate({-rotacao})" if rotacao else ""
        self.saida.append(
            f'<text transform="translate({x:.2f},{y:.2f}){giro}" y="{dy:.2f}" '
            f'text-anchor="{ancora}" style="{";".join(estilo)}">{com_barra(conteudo)}</text>')

    def texto_do_registro(self, reg, conteudo):
        just = int(reg.get("Justification", 0) or 0)
        x, y = self.ponto(reg)
        fonte = self.fontes.get(reg.get("FontID", "1"), {})
        rotacao = int(reg.get("Orientation", 0) or 0) * 90 or fonte.get("rotacao", 0)
        self.texto(x, y, conteudo, reg.get("FontID", "1"), cor(reg), rotacao,
                   ("start", "middle", "end")[just % 3],
                   ("base", "meio", "topo")[min(just // 3, 2)])

    def linha(self, pts, cor_, espessura, fechar=False, fundo="none", extra=""):
        if len(pts) < 2:
            return
        d = "M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in pts) + (" Z" if fechar else "")
        self.saida.append(
            f'<path d="{d}" fill="{fundo}" stroke="{cor_}" '
            f'stroke-width="{espessura}" stroke-linecap="round" '
            f'stroke-linejoin="round"{extra}/>')

    # -------------------------------------------------------------- registros

    def arco(self, reg, dono):
        cx, cy = self.ponto(reg)
        rx = num(reg, "Radius")
        ry = num(reg, "SecondaryRadius", rx) if reg.get("RECORD") == "11" else rx
        a0, a1 = num(reg, "StartAngle", 0.0), num(reg, "EndAngle", 360.0)
        self.marcar(dono, (cx - rx, cy - ry), (cx + rx, cy + ry))
        traco = f'fill="none" stroke="{cor(reg)}" stroke-width="{self.espessura(reg)}"'
        varredura = (a1 - a0) % 360
        if varredura == 0:
            self.saida.append(f'<ellipse cx="{cx:.2f}" cy="{cy:.2f}" rx="{rx:.2f}" '
                              f'ry="{ry:.2f}" {traco}/>')
            return
        x0 = cx + rx * math.cos(math.radians(a0))
        y0 = cy - ry * math.sin(math.radians(a0))
        x1 = cx + rx * math.cos(math.radians(a1))
        y1 = cy - ry * math.sin(math.radians(a1))
        self.saida.append(
            f'<path d="M{x0:.2f},{y0:.2f} A{rx:.2f},{ry:.2f} 0 '
            f'{1 if varredura > 180 else 0} 0 {x1:.2f},{y1:.2f}" {traco}/>')

    def pino(self, reg, dono):
        flags = int(reg.get("PinConglomerate", 0) or 0)
        if flags & 4:
            return
        x, y = self.ponto(reg)
        dx, dy = ((1, 0), (0, -1), (-1, 0), (0, 1))[flags & 3]
        comprimento = num(reg, "PinLength", 30)
        fx, fy = x + dx * comprimento, y + dy * comprimento
        self.linha([(x, y), (fx, fy)], cor(reg), 1)
        self.marcar(dono, (x, y), (fx, fy))
        vertical = dx == 0
        if flags & 8 and reg.get("Name"):
            # nome dentro do corpo, encostado na raiz do pino
            self.texto(x - dx * 5, y - dy * 5, reg["Name"],
                       reg.get("Name_CustomFontID", "1"), "#000000",
                       90 if vertical else 0,
                       "end" if (dx > 0 or dy < 0) else "start", "meio")
        if flags & 16 and reg.get("Designator"):
            # numero fora do corpo, sobre a linha do pino
            self.texto(x + dx * 8 - (1.5 if vertical else 0),
                       y + dy * 8 - (0 if vertical else 1.5), reg["Designator"],
                       reg.get("Designator_CustomFontID", "1"), "#000000",
                       90 if vertical else 0,
                       "start" if (dx > 0 or dy < 0) else "end", "base")

    def porta(self, reg):
        x, y = self.ponto(reg)
        dx, dy = ((1, 0), (0, -1), (-1, 0), (0, 1))[int(reg.get("Orientation", 0) or 0) & 3]
        px, py = -dy, dx
        estilo = int(reg.get("Style", 0) or 0)
        c = cor(reg)

        def seg(dist, meia):
            self.linha([(x + dx * dist - px * meia, y + dy * dist - py * meia),
                        (x + dx * dist + px * meia, y + dy * dist + py * meia)], c, 1)

        fim = 10
        self.linha([(x, y), (x + dx * 10, y + dy * 10)], c, 1)
        if estilo == 4:        # terra de potencia: tres barras
            for dist, meia in ((10, 10), (13, 7), (16, 4), (19, 1)):
                seg(dist, meia)
            fim = 20
        elif estilo == 5:      # terra de sinal: triangulo
            self.linha([(x + dx * 10 - px * 10, y + dy * 10 - py * 10),
                        (x + dx * 10 + px * 10, y + dy * 10 + py * 10),
                        (x + dx * 20, y + dy * 20)], c, 1, fechar=True)
            fim = 20
        elif estilo == 6:      # terra de chassi
            seg(10, 10)
            for desloc in (-10, 0, 10):
                self.linha([(x + dx * 10 + px * desloc, y + dy * 10 + py * desloc),
                            (x + dx * 20 + px * (desloc - 5), y + dy * 20 + py * (desloc - 5))],
                           c, 1)
            fim = 20
        elif estilo == 0:      # circulo
            self.saida.append(f'<circle cx="{x + dx * 13:.2f}" cy="{y + dy * 13:.2f}" r="3" '
                              f'fill="none" stroke="{c}"/>')
            fim = 16
        elif estilo == 1:      # seta
            self.linha([(x + dx * 10 - px * 4, y + dy * 10 - py * 4),
                        (x + dx * 17, y + dy * 17),
                        (x + dx * 10 + px * 4, y + dy * 10 + py * 4)], c, 1)
            fim = 17
        else:                  # barra
            seg(10, 5 if estilo == 2 else 7)
        if reg.get("ShowNetName", "T") == "T" and reg.get("Text"):
            tx, ty = x + dx * (fim + 3), y + dy * (fim + 3)
            if dy:
                self.texto(tx, ty, reg["Text"], reg.get("FontID", "1"), c, 0, "middle",
                           "base" if dy < 0 else "topo")
            else:
                self.texto(tx, ty, reg["Text"], reg.get("FontID", "1"), c, 0,
                           "start" if dx > 0 else "end", "meio")

    # ------------------------------------------------- esquema hierarquico

    def simbolo_de_folha(self, reg):
        """RECORD=15: o retangulo que representa uma folha filha.

        Location e o canto superior esquerdo; o simbolo desce YSize.
        """
        x, y = self.ponto(reg)
        larg, alt = num(reg, "XSize"), num(reg, "YSize")
        fundo = cor(reg, "AreaColor", "#ffffff") if reg.get("IsSolid") == "T" else "none"
        self.saida.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{larg:.2f}" height="{alt:.2f}" '
            f'fill="{fundo}" stroke="{cor(reg)}" stroke-width="{self.espessura(reg)}"/>')

    def entrada_de_folha(self, reg, simbolo):
        """RECORD=16: entrada na borda do simbolo de folha, com o nome do sinal.

        Side: 0 esquerda, 1 direita, 2 topo, 3 base. DistanceFromTop, em passos
        de 10 unidades, conta do topo (lados) ou da esquerda (topo e base).
        """
        if not simbolo or simbolo.get("RECORD") != "15":
            return
        x0, y0 = self.ponto(simbolo)
        larg, alt = num(simbolo, "XSize"), num(simbolo, "YSize")
        lado = int(reg.get("Side", 0) or 0)
        # o Altium guarda a fracao em DistanceFromTop_Frac1 (e nao em _Frac)
        dist = 10 * (num(reg, "DistanceFromTop")
                     + float(reg.get("DistanceFromTop_Frac1", 0) or 0) / 1e5)
        c, fundo = cor(reg), cor(reg, "AreaColor", "#ffffff")
        cor_texto = cor(reg, "TextColor", c)
        fonte = reg.get("TextFontID", "1")
        nome = reg.get("Name", "")
        comp, meia = 12, 4  # tamanho da marca, em unidades da folha
        if lado in (0, 1):
            y = y0 + dist
            xa = x0 if lado == 0 else x0 + larg - comp
            self.linha([(xa, y - meia), (xa + comp, y - meia), (xa + comp, y + meia),
                        (xa, y + meia)], c, 1, fechar=True, fundo=fundo)
            if lado == 0:
                self.texto(x0 + comp + 3, y, nome, fonte, cor_texto, 0, "start", "meio")
            else:
                self.texto(x0 + larg - comp - 3, y, nome, fonte, cor_texto, 0, "end", "meio")
        else:
            x = x0 + dist
            ya = y0 if lado == 2 else y0 + alt - comp
            self.linha([(x - meia, ya), (x + meia, ya), (x + meia, ya + comp),
                        (x - meia, ya + comp)], c, 1, fechar=True, fundo=fundo)
            if lado == 2:
                self.texto(x, y0 + comp + 3, nome, fonte, cor_texto, 90, "end", "meio")
            else:
                self.texto(x, y0 + alt - comp - 3, nome, fonte, cor_texto, 90, "start",
                           "meio")

    def porta_de_folha(self, reg):
        """RECORD=18: porta que liga a folha ao simbolo dela na folha de cima.

        Location e o meio da ponta inicial. Style 0 a 3 e horizontal (cresce
        para a direita por Width), 4 a 7 e vertical (cresce para cima). A ponta
        em seta fica: 1 esquerda, 2 direita, 3 as duas; 5 topo, 6 base, 7 as duas.
        Alignment do nome: 1 no inicio, 2 no centro, 3 no fim.
        """
        x, y = self.ponto(reg)
        larg, meia = num(reg, "Width"), num(reg, "Height", 10.0) / 2
        estilo = int(reg.get("Style", 0) or 0)
        vertical = estilo >= 4
        ponta_ini = estilo in (1, 3, 6, 7)
        ponta_fim = estilo in (2, 3, 5, 7)
        a = meia if ponta_ini else 0
        b = larg - (meia if ponta_fim else 0)
        local = ([(a, -meia), (b, -meia)] + ([(larg, 0)] if ponta_fim else [])
                 + [(b, meia), (a, meia)] + ([(0, 0)] if ponta_ini else []))
        if vertical:
            pts = [(x + v, y - u) for u, v in local]
        else:
            pts = [(x + u, y + v) for u, v in local]
        c = cor(reg)
        self.linha(pts, c, 1, fechar=True, fundo=cor(reg, "AreaColor", "#ffffff"))
        alinhamento = int(reg.get("Alignment", 2) or 2)
        folga = meia + 2
        desloc, ancora = {1: (folga, "start"), 3: (larg - folga, "end")}.get(
            alinhamento, (larg / 2, "middle"))
        cor_texto = cor(reg, "TextColor", c)
        if vertical:
            self.texto(x, y - desloc, reg.get("Name", ""), reg.get("FontID", "1"),
                       cor_texto, 90, ancora, "meio")
        else:
            self.texto(x + desloc, y, reg.get("Name", ""), reg.get("FontID", "1"),
                       cor_texto, 0, ancora, "meio")

    def imagem(self, reg, embutidas):
        x0, y0 = self.ponto(reg)
        x1, y1 = self.ponto(reg, "Corner")
        x, y, larg, alt = min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)
        nome = reg.get("FileName", "")
        dados = embutidas.get(nome)
        if dados is None:
            base = nome.replace("\\", "/").rsplit("/", 1)[-1]
            externo = self.externos.get(base)
            if externo and os.path.exists(externo):
                dados = open(externo, "rb").read()
        if dados is None:
            return
        self.saida.append(
            f'<image x="{x:.2f}" y="{y:.2f}" width="{larg:.2f}" height="{alt:.2f}" '
            f'preserveAspectRatio="xMidYMid meet" href="{uri_da_imagem(dados)}"/>')

    # ----------------------------------------------------------------- folha

    def moldura(self):
        fundo = "#ffffff"  # impressao: papel branco, nao a cor de tela da folha
        self.saida.append(f'<rect width="{self.L}" height="{self.A}" fill="{fundo}"/>')
        if self.folha.get("BorderOn", "T") != "T":
            return
        m = 20
        for desloc, esp in ((0.5, 1), (m, 1)):
            self.saida.append(
                f'<rect x="{desloc}" y="{desloc}" width="{self.L - 2 * desloc}" '
                f'height="{self.A - 2 * desloc}" fill="none" stroke="#000000" '
                f'stroke-width="{esp}"/>')
        # zonas de referencia: numeros em cima e embaixo, letras nas laterais
        nx, ny = max(2, round(self.L / 200)), max(2, round(self.A / 280))
        for i in range(nx):
            x0 = m + (self.L - 2 * m) * i / nx
            meio = x0 + (self.L - 2 * m) / nx / 2
            for yb, yt in ((0.5, m), (self.A - m, self.A - 0.5)):
                if i:
                    self.linha([(x0, yb), (x0, yt)], "#000000", 0.5)
                self.texto(meio, (yb + yt) / 2, str(i + 1), "1", "#000000", 0,
                           "middle", "meio")
        for j in range(ny):
            y0 = m + (self.A - 2 * m) * j / ny
            meio = y0 + (self.A - 2 * m) / ny / 2
            for xa, xb in ((0.5, m), (self.L - m, self.L - 0.5)):
                if j:
                    self.linha([(xa, y0), (xb, y0)], "#000000", 0.5)
                self.texto((xa + xb) / 2, meio, chr(65 + j), "1", "#000000", 0,
                           "middle", "meio")

    def desenhar(self):
        recs = self.recs
        comps = {i: r for i, r in enumerate(recs) if r.get("RECORD") == "1"}
        designador = {}
        irmaos = {}
        for reg in recs:
            dono = reg.get("OwnerIndex")
            if dono is None or not dono.lstrip("-").isdigit():
                continue
            if reg.get("RECORD") == "34":
                designador[int(dono)] = reg.get("Text", "")
            elif reg.get("RECORD") == "41" and reg.get("Name"):
                irmaos.setdefault(int(dono), {})[reg["Name"].lower()] = reg.get("Text", "")

        embutidas = esquema.imagens_embutidas(self.caminho)
        self.moldura()
        juncoes = []
        for reg in recs:
            tipo = reg.get("RECORD")
            dono = reg.get("OwnerIndex")
            dono = int(dono) if dono and dono.lstrip("-").isdigit() else None
            comp = comps.get(dono)
            if comp is not None and tipo not in ("34", "41"):
                # so a parte e o modo de exibicao correntes do simbolo
                parte = reg.get("OwnerPartId", "-1")
                if parte not in ("-1", "0", comp.get("CurrentPartId", "1")):
                    continue
                if reg.get("OwnerPartDisplayMode", "0") != comp.get("DisplayMode", "0"):
                    continue
            marca = dono if comp is not None else None

            if tipo == "27":                                    # fio
                self.linha(self.pontos(reg), cor(reg, padrao="#000080"),
                           self.espessura(reg))
            elif tipo == "26":                                  # barramento
                self.linha(self.pontos(reg), cor(reg, padrao="#000080"), 3)
            elif tipo == "6":                                   # polilinha
                pts = self.pontos(reg)
                self.marcar(marca, *pts)
                self.linha(pts, cor(reg), self.espessura(reg))
            elif tipo == "13":                                  # linha
                pts = [self.ponto(reg), self.ponto(reg, "Corner")]
                self.marcar(marca, *pts)
                self.linha(pts, cor(reg), self.espessura(reg))
            elif tipo == "7":                                   # poligono
                pts = self.pontos(reg)
                self.marcar(marca, *pts)
                fundo = cor(reg, "AreaColor", "#ffffff") if reg.get("IsSolid") == "T" else "none"
                self.linha(pts, cor(reg), self.espessura(reg), fechar=True, fundo=fundo)
            elif tipo in ("14", "10"):                          # retangulo
                (x0, y0), (x1, y1) = self.ponto(reg), self.ponto(reg, "Corner")
                self.marcar(marca, (x0, y0), (x1, y1))
                fundo = cor(reg, "AreaColor", "#ffffff") if reg.get("IsSolid") == "T" else "none"
                self.saida.append(
                    f'<rect x="{min(x0, x1):.2f}" y="{min(y0, y1):.2f}" '
                    f'width="{abs(x1 - x0):.2f}" height="{abs(y1 - y0):.2f}" '
                    f'fill="{fundo}" stroke="{cor(reg)}" stroke-width="{self.espessura(reg)}"/>')
            elif tipo == "8":                                   # elipse
                cx, cy = self.ponto(reg)
                rx, ry = num(reg, "Radius"), num(reg, "SecondaryRadius")
                self.marcar(marca, (cx - rx, cy - ry), (cx + rx, cy + ry))
                fundo = cor(reg, "AreaColor", "#ffffff") if reg.get("IsSolid") == "T" else "none"
                self.saida.append(
                    f'<ellipse cx="{cx:.2f}" cy="{cy:.2f}" rx="{rx:.2f}" ry="{ry:.2f}" '
                    f'fill="{fundo}" stroke="{cor(reg)}" stroke-width="{self.espessura(reg)}"/>')
            elif tipo in ("11", "12"):                          # arco
                self.arco(reg, marca)
            elif tipo == "2":                                   # pino
                self.pino(reg, marca)
            elif tipo == "17":                                  # porta de alimentacao
                self.porta(reg)
            elif tipo == "29":                                  # juncao
                juncoes.append((self.ponto(reg), cor(reg, padrao="#000080")))
            elif tipo == "30":                                  # imagem
                self.imagem(reg, embutidas)
            elif tipo in ("4", "25"):                           # texto, net label
                self.texto_do_registro(reg, self.resolver(reg.get("Text", "")))
            elif tipo == "34":                                  # designador
                if reg.get("IsHidden") != "T":
                    self.texto_do_registro(reg, reg.get("Text", ""))
            elif tipo == "41":                                  # parametro visivel
                if reg.get("IsHidden") != "T" and dono is not None:
                    self.texto_do_registro(
                        reg, self.resolver(reg.get("Text", ""), irmaos.get(dono)))
            elif tipo == "15":                                  # simbolo de folha
                self.simbolo_de_folha(reg)
            elif tipo == "16":                                  # entrada de folha
                self.entrada_de_folha(
                    reg, recs[dono] if dono is not None and 0 <= dono < len(recs) else None)
            elif tipo in ("32", "33"):                          # nome e arquivo da folha
                if reg.get("IsHidden") != "T":
                    self.texto_do_registro(reg, reg.get("Text", ""))
            elif tipo == "18":                                  # porta
                self.porta_de_folha(reg)
            elif tipo == "22":                                  # no ERC
                x, y = self.ponto(reg)
                self.linha([(x - 3, y - 3), (x + 3, y + 3)], "#ff0000", 1)
                self.linha([(x - 3, y + 3), (x + 3, y - 3)], "#ff0000", 1)

        for (x, y), c in juncoes:
            self.saida.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2" fill="{c}"/>')

        for indice, caixa in self.caixas.items():
            if designador.get(indice) in self.nao_montados and caixa[2] > caixa[0] - 1:
                x0, y0, x1, y1 = caixa[0] - 3, caixa[1] - 3, caixa[2] + 3, caixa[3] + 3
                self.linha([(x0, y0), (x1, y1)], "#ff0000", 1.5)
                self.linha([(x0, y1), (x1, y0)], "#ff0000", 1.5)

        # largura em mm reais: 1 unidade = 0,254 mm
        return "\n".join([
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.L * 0.254:.1f}mm" '
            f'height="{self.A * 0.254:.1f}mm" viewBox="0 0 {self.L} {self.A}">',
            *self.saida, "</svg>", ""])


def imprimir(schdoc, destino, nao_montados=(), data=None, arquivos_externos=None):
    svg = Desenho(schdoc, nao_montados, data, arquivos_externos).desenhar()
    with open(destino, "w", encoding="utf-8") as fp:
        fp.write(svg)
    return destino


if __name__ == "__main__":
    imprimir(sys.argv[1], sys.argv[2], sys.argv[3].split(",") if len(sys.argv) > 3 else (),
             arquivos_externos={"logo_dextro_labs.png": os.path.join(
                 os.path.dirname(os.path.abspath(__file__)), "logo_dextro_labs.png")})
