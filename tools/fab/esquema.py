#!/usr/bin/env python3
"""Le os arquivos do Altium sem o Altium: .SchDoc, .PcbDoc e .PrjPcb.

O KiCad nao ajuda no esquema: o importador de .SchDoc dele so existe na GUI.
Mas o .SchDoc e um container OLE cujo stream 'FileHeader' guarda os registros
como texto 'CHAVE=valor' separado por '|', entao da pra ler direto.

Registros que interessam para a BOM:
    RECORD=1    componente (LibReference, ComponentDescription, UniqueID)
    RECORD=34   designador do componente (Text=R1), ligado por OwnerIndex
    RECORD=41   parametro nomeado (Name=LCSC, Text=C17477), idem
    RECORD=45   modelo associado; o footprint e o de ModelType=PCBLIB corrente

OwnerIndex e a posicao ordinal do registro dono dentro do stream, sem contar o
cabecalho.
"""

import os
import re
import struct

FIM_DE_CADEIA = 0xFFFFFFFE
LIVRE = 0xFFFFFFFF


# ---------------------------------------------------------------- container

def ler_streams(caminho):
    """Devolve {caminho/do/stream: bytes} de um arquivo OLE (compound file)."""
    dados = open(caminho, "rb").read()
    if dados[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise ValueError(f"{caminho} nao e um container OLE")
    tam_setor = 1 << struct.unpack_from("<H", dados, 0x1E)[0]
    tam_mini = 1 << struct.unpack_from("<H", dados, 0x20)[0]
    (_n_fat, prim_dir, _tx, corte, prim_minifat, _n_minifat,
     prim_difat, _n_difat) = struct.unpack_from("<8I", dados, 0x2C)

    def setor(n):
        inicio = (n + 1) * tam_setor
        return dados[inicio:inicio + tam_setor]

    por_setor = tam_setor // 4
    difat = list(struct.unpack_from("<109I", dados, 0x4C))
    prox, vistos = prim_difat, set()
    while prox not in (FIM_DE_CADEIA, LIVRE) and prox not in vistos:
        vistos.add(prox)
        bloco = struct.unpack(f"<{por_setor}I", setor(prox))
        difat += bloco[:-1]
        prox = bloco[-1]
    fat = []
    for n in difat:
        if n not in (FIM_DE_CADEIA, LIVRE):
            fat += struct.unpack(f"<{por_setor}I", setor(n))

    def cadeia(inicio, tabela):
        saida, vistos = [], set()
        while inicio < len(tabela) and inicio not in vistos:
            vistos.add(inicio)
            saida.append(inicio)
            inicio = tabela[inicio]
        return saida

    def ler_grande(inicio, tamanho=None):
        bruto = b"".join(setor(n) for n in cadeia(inicio, fat))
        return bruto if tamanho is None else bruto[:tamanho]

    diretorio = ler_grande(prim_dir)
    entradas = []
    for pos in range(0, len(diretorio) - 127, 128):
        ent = diretorio[pos:pos + 128]
        n_nome = struct.unpack_from("<H", ent, 0x40)[0]
        if n_nome < 2:
            entradas.append(None)
            continue
        esq, dir_, filho = struct.unpack_from("<3I", ent, 0x44)
        entradas.append({
            "nome": ent[:n_nome - 2].decode("utf-16-le", "replace"),
            "tipo": ent[0x42], "esq": esq, "dir": dir_, "filho": filho,
            "inicio": struct.unpack_from("<I", ent, 0x74)[0],
            "tamanho": struct.unpack_from("<I", ent, 0x78)[0],
        })
    if not entradas or not entradas[0]:
        return {}

    raiz = entradas[0]
    mini_stream = ler_grande(raiz["inicio"], raiz["tamanho"])
    minifat = []
    if prim_minifat not in (FIM_DE_CADEIA, LIVRE):
        bruto = ler_grande(prim_minifat)
        minifat = list(struct.unpack(f"<{len(bruto) // 4}I", bruto))

    def conteudo(ent):
        if ent["tamanho"] >= corte:
            return ler_grande(ent["inicio"], ent["tamanho"])
        partes = [mini_stream[n * tam_mini:(n + 1) * tam_mini]
                  for n in cadeia(ent["inicio"], minifat)]
        return b"".join(partes)[:ent["tamanho"]]

    streams = {}

    def andar(indice, prefixo, vistos):
        # os irmaos de um diretorio formam uma arvore binaria (esq/dir)
        if indice == LIVRE or indice >= len(entradas) or indice in vistos:
            return
        vistos.add(indice)
        ent = entradas[indice]
        if not ent:
            return
        andar(ent["esq"], prefixo, vistos)
        andar(ent["dir"], prefixo, vistos)
        nome = prefixo + ent["nome"]
        if ent["tipo"] == 2:
            streams[nome] = conteudo(ent)
        elif ent["tipo"] == 1:
            andar(ent["filho"], nome + "/", vistos)

    andar(raiz["filho"], "", set())
    return streams


class Campos(dict):
    """Registro com chave sem caixa: 'OwnerIndex' e 'OWNERINDEX' sao a mesma.

    Ha versoes do Altium que gravam as chaves em maiusculas (RECORD=1|OWNERINDEX=..)
    e outras em CamelCase (OwnerIndex=..); o codigo consulta em CamelCase.
    """

    def __init__(self, *args, **kw):
        super().__init__()
        self.update(*args, **kw)

    def __setitem__(self, chave, valor):
        super().__setitem__(chave.lower(), valor)

    def __getitem__(self, chave):
        return super().__getitem__(chave.lower())

    def __contains__(self, chave):
        return super().__contains__(chave.lower())

    def get(self, chave, padrao=None):
        return super().get(chave.lower(), padrao)

    def setdefault(self, chave, padrao=None):
        return super().setdefault(chave.lower(), padrao)

    def pop(self, chave, *padrao):
        return super().pop(chave.lower(), *padrao)

    def update(self, *args, **kw):
        for chave, valor in dict(*args, **kw).items():
            self[chave] = valor


def _campos(bruto):
    """'|A=1|%UTF8%A=1|B=2' -> {'A': '1', 'B': '2'}; a copia UTF-8 tem prioridade.

    As chaves nao ligam para maiusculas (ver Campos).
    """
    campos, utf8 = Campos(), {}
    for parte in bruto.split(b"|"):
        chave, igual, valor = parte.partition(b"=")
        if not igual:
            continue
        chave = chave.decode("latin1").strip()
        if chave.upper().startswith("%UTF8%"):
            utf8[chave[6:]] = valor.decode("utf-8", "replace").rstrip("\x00")
        else:
            campos[chave] = valor.decode("cp1252", "replace").rstrip("\x00")
    campos.update(utf8)
    return campos


def registros(caminho):
    """Registros do stream FileHeader do .SchDoc, na ordem, sem o cabecalho."""
    try:
        stream = ler_streams(caminho)["FileHeader"]
    except (ValueError, KeyError, struct.error):
        # arquivo fora do padrao: cai para a varredura do binario inteiro
        dados = open(caminho, "rb").read()
        return [_campos(a.group(0))
                for a in re.finditer(rb"\|RECORD=[^\x00]{0,8000}", dados)]

    saida, pos = [], 0
    while pos + 4 <= len(stream):
        tamanho = struct.unpack_from("<I", stream, pos)[0]
        binario, tamanho = tamanho >> 24, tamanho & 0xFFFFFF
        corpo = stream[pos + 4:pos + 4 + tamanho]
        pos += 4 + tamanho
        if binario:
            saida.append(Campos(RECORD="binario"))
            continue
        campos = _campos(corpo.rstrip(b"\x00"))
        if "HEADER" in campos and "RECORD" not in campos:
            continue
        saida.append(campos)
    return saida


def imagens_embutidas(caminho):
    """{nome do arquivo: bytes da imagem} do stream Storage do .SchDoc."""
    import zlib
    try:
        stream = ler_streams(caminho)["Storage"]
    except (ValueError, KeyError, struct.error):
        return {}
    saida, pos = {}, 0
    while pos + 4 <= len(stream):
        tamanho = struct.unpack_from("<I", stream, pos)[0]
        binario, tamanho = tamanho >> 24, tamanho & 0xFFFFFF
        corpo = stream[pos + 4:pos + 4 + tamanho]
        pos += 4 + tamanho
        if not binario or len(corpo) < 6 or corpo[0] != 0xD0:
            continue
        n_nome = corpo[1]
        nome = corpo[2:2 + n_nome].decode("cp1252", "replace")
        n_dados = struct.unpack_from("<I", corpo, 2 + n_nome)[0]
        try:
            saida[nome] = zlib.decompress(corpo[6 + n_nome:6 + n_nome + n_dados])
        except zlib.error:
            pass
    return saida


# --------------------------------------------------------------- componentes

def componentes(caminho):
    """Devolve [{designador, comentario, descricao, lcsc, footprint, ...}]."""
    recs = registros(caminho)
    donos = {i: r for i, r in enumerate(recs) if r.get("RECORD") == "1"}

    designadores, parametros, footprints = {}, {}, {}
    dono_do_modelo = None
    for reg in recs:
        tipo = reg.get("RECORD")
        indice = reg.get("OwnerIndex")
        if indice is None or not indice.lstrip("-").isdigit():
            continue
        indice = int(indice)
        if tipo == "34":
            designadores[indice] = reg.get("Text", "")
        elif tipo == "41" and reg.get("Name"):
            parametros.setdefault(indice, Campos())[reg["Name"]] = reg.get("Text", "")
        elif tipo == "44":
            # lista de implementacoes: os RECORD=45 seguintes pertencem a ela
            dono_do_modelo = indice
        elif tipo == "45" and reg.get("ModelType", "").upper() == "PCBLIB":
            dono = dono_do_modelo if dono_do_modelo in donos else indice
            if reg.get("IsCurrent") == "T" or dono not in footprints:
                footprints[dono] = reg.get("ModelName", "")

    saida = []
    for indice, comp in sorted(donos.items()):
        params = parametros.get(indice, Campos())
        try:
            partes = int(comp.get("PartCount", "1"))
        except ValueError:
            partes = 1
        comentario = params.get("Comment", "") or comp.get("LibReference", "")
        if comentario.startswith("="):
            comentario = params.get(comentario[1:], comentario)
        lcsc = params.get("LCSC", "").strip()
        if lcsc in ("", "*") and params.get("Supplier", "").upper() == "LCSC":
            # o nome do parametro varia com a biblioteca de origem
            lcsc = (params.get("SupplierPart") or params.get("Supplier Part")
                    or "").strip()
        saida.append({
            "partes": partes,
            "designador": designadores.get(indice, ""),
            "comentario": comentario,
            "biblioteca": comp.get("LibReference", ""),
            "descricao": comp.get("ComponentDescription", ""),
            "lcsc": "" if lcsc == "*" else lcsc,
            "footprint": footprints.get(indice, ""),
            "uniqueid": comp.get("UniqueID", ""),
            "parametros": params,
        })
    return [c for c in saida if c["designador"]]


# ------------------------------------------------------- folhas e variantes

def folhas_do_projeto(prjpcb):
    """Folhas .SchDoc do projeto, na ordem em que o .PrjPcb as lista.

    Num esquema hierarquico a folha de topo vem primeiro, como na impressao do
    Altium. Os caminhos saem relativos a pasta do .PrjPcb.
    """
    base = os.path.dirname(prjpcb)
    saida = []
    for linha in open(prjpcb, encoding="cp1252", errors="replace"):
        linha = linha.strip()
        if linha.startswith("DocumentPath=") and linha.lower().endswith(".schdoc"):
            relativo = linha.partition("=")[2].replace("\\", "/")
            caminho = os.path.normpath(os.path.join(base, relativo))
            if caminho not in saida:
                saida.append(caminho)
    return saida


def variantes(prjpcb):
    """{nome da variante: designadores nao montados}, lido do .PrjPcb.

    Kind=1 e 'not fitted'. Variacao com peca alternativa (Kind=2) nao e
    tratada aqui: o componente continua montado, com a peca do esquema.
    """
    saida, atual = {}, None
    for linha in open(prjpcb, encoding="cp1252", errors="replace"):
        linha = linha.strip()
        if linha.startswith("["):
            atual = None
            if linha.startswith("[ProjectVariant"):
                atual = {"nome": "", "fora": set()}
        elif atual is not None:
            if linha.startswith("Description=") and not atual["nome"]:
                atual["nome"] = linha.partition("=")[2]
                saida[atual["nome"]] = atual["fora"]
            elif re.match(r"Variation\d+=", linha):
                campos = dict(p.partition("=")[::2]
                              for p in linha.partition("=")[2].split("|"))
                if campos.get("Kind") == "1" and campos.get("Designator"):
                    atual["fora"].add(campos["Designator"])
    return saida


def footprints_do_pcb(pcbdoc):
    """{designador: footprint} lido direto do .PcbDoc, sem KiCad."""
    try:
        stream = ler_streams(pcbdoc)["Components6/Data"]
    except (ValueError, KeyError, struct.error):
        return {}
    saida, pos = {}, 0
    while pos + 4 <= len(stream):
        tamanho = struct.unpack_from("<I", stream, pos)[0]
        campos = _campos(stream[pos + 4:pos + 4 + tamanho].rstrip(b"\x00"))
        pos += 4 + tamanho
        if campos.get("SOURCEDESIGNATOR"):
            saida[campos["SOURCEDESIGNATOR"]] = campos.get("PATTERN", "")
    return saida


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(componentes(sys.argv[1]), indent=2, ensure_ascii=False))
