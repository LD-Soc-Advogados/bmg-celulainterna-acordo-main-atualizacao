"""
Blacklist compartilhada de advogados do Banco/socios LD.

Fonte unica: `blacklist_advogados.txt` na raiz do projeto (editavel pela
GUI ou direto no arquivo). Formato:

    NOME | OAB | UF        (# = comentario, linhas vazias ignoradas)

Match:
    - Nome parcial, SEM acento, uppercase. 'ANTONIO' casa 'ANTÔNIO'.
    - OAB por digitos so.
    - Basta UM dos dois casar (nome ou OAB) pra excluir.

Usado por:
    - fluxos/consultar_djen.py       (advogados extraidos das comunicacoes)
    - core/extrator_contatos.py      (advogados extraidos dos PDFs)
    - qualquer script ou varredura nova: importe daqui, NAO reimplemente.

Por que existe:
    Ate 2026-08 cada consumidor mantinha sua propria copia da blacklist
    (hardcoded no extrator_contatos, arquivo lido no consultar_djen). Um
    advogado adicionado pela GUI so afetava o DJEN - o extrator de PDF
    seguia capturando ele como se fosse do Autor. Consequencia real: 34+
    registros de advogados do BMG entraram na planilha de "reincidentes
    do Autor" numa varredura ad hoc, e teriam contaminado o CNA.
"""
import re
import unicodedata
from pathlib import Path


BLACKLIST_PATH = Path(__file__).resolve().parent.parent / "blacklist_advogados.txt"


# Defaults se o arquivo nao existir ou vier corrompido: os 3 socios LD.
# (nome_parcial_upper, oab_digitos)
_DEFAULT = [
    ("KELI LUCCHESI", "90395"),
    ("LOUIS DOLABELA", "124826"),
    ("ADRIANO LUCCHESI", "176171"),
]


def _sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c))


def carregar_blacklist():
    """Le `blacklist_advogados.txt` -> list[(nome_upper, oab_digitos)].

    Sem cache: recarrega toda chamada. O arquivo eh pequeno (dezenas de
    linhas) e permite editar entre CIVs sem reiniciar o robo.
    """
    if not BLACKLIST_PATH.exists():
        return list(_DEFAULT)
    entradas = []
    try:
        for linha in BLACKLIST_PATH.read_text(encoding="utf-8").splitlines():
            s = linha.strip()
            if not s or s.startswith("#"):
                continue
            partes = [p.strip() for p in s.split("|")]
            nome = partes[0].upper() if partes else ""
            oab = re.sub(r"[^0-9]", "", partes[1]) if len(partes) > 1 else ""
            if nome or oab:
                entradas.append((nome, oab))
    except Exception:
        return list(_DEFAULT)
    return entradas or list(_DEFAULT)


def esta_no_bmg(nome: str, oab: str, blacklist=None) -> bool:
    """True se (nome parcial OU oab) casa alguma entrada da blacklist.

    Passe `blacklist=` pra reusar a lista pre-carregada (evita reler o
    arquivo N vezes num loop). Chamadas ocasionais podem omitir.
    """
    if blacklist is None:
        blacklist = carregar_blacklist()
    nome_up = _sem_acento(nome).upper()
    oab_digits = re.sub(r"[^\d]", "", oab or "")
    for item_nome, item_oab in blacklist:
        if item_nome and _sem_acento(item_nome).upper() in nome_up:
            return True
        if item_oab and oab_digits and oab_digits == item_oab:
            return True
    return False
