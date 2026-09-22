"""
Extrator de info do(s) advogado(s) do Autor a partir de um PDF.

Estrategia:
  1. Tenta extrair TEXTO direto via pdfplumber.
  2. Se texto < OCR_THRESHOLD_CHARS, marca como `ocr_necessario=True`
     e deixa pra Fase G2 (Tesseract).
  3. Aplica regex robusta pra OAB, e-mail, telefone, celular.
  4. Heuristica pra nome do advogado:
     - Procura linhas com OAB
     - Pega linha imediatamente acima (CAPS ou Capitalized)
     - Filtra blacklist (palavras que NAO sao nome de pessoa)

Locais de busca preferenciais (em ordem):
  - Timbre / cabecalho (1a pagina) - escritorios sempre poem OAB+email
  - Procuracao (1-2 paginas, geralmente final do PDF)
  - Final da petição (antes de "Pede Deferimento")
  - Qualificação da parte autora (e-mail incomum)
"""
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from core.blacklist import carregar_blacklist, esta_no_bmg

try:
    import pdfplumber
except ImportError:
    pdfplumber = None  # Tratamento defensivo - extrator nao roda sem


# Texto extraido com menos chars que isso -> PDF eh imagem (OCR necessario)
OCR_THRESHOLD_CHARS = 500


# Regexes
RE_OAB = re.compile(
    r"\bOAB\s*[/\-\s]?\s*([A-Z]{2})?\s*(?:n[º°\.]?|N[º°\.]?)?\s*"
    r"([\d][\d\.\s]{3,8}[\d])(?:\s*[/\-]\s*([A-Z]))?",
    re.IGNORECASE,
)
RE_EMAIL = re.compile(
    r"\b([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})\b"
)
RE_TELEFONE = re.compile(
    r"\(?(\d{2})\)?\s*[-.\s]?\s*"
    r"(9?\d{4})\s*[-.\s]?\s*(\d{4})\b"
)
# Telefone CORRIDO sem separador: 10 ou 11 digitos colados
# (formato comum em timbre/rodape: 5133275533, 51999996666)
RE_TELEFONE_CORRIDO = re.compile(r"(?<!\d)(\d{10,11})(?!\d)")

# Celular com 9 separado por ponto: (48) 9.9941.3571 ou 48 9.9941.3571.
# Visto em timbre de advogado (CIV1348175 - RAFAEL MARTIGNAGO).
# Tres grupos: DDD, 4 digitos (apos o 9.), 4 digitos finais.
RE_TELEFONE_CEL_PONTO = re.compile(
    r"\(?(\d{2})\)?\s*9\s*\.\s*(\d{4})\s*\.\s*(\d{4})\b"
)


# --- BLACKLISTS ---
# Blacklist de advogados do Banco (socios LD + bancas conhecidas): fonte
# unica em core.blacklist (arquivo `blacklist_advogados.txt` editavel pela
# GUI). Ate 2026-08 este modulo tinha copia hardcoded so dos socios, o que
# fazia bancas do Banco (Sonntag, FMD/Eugenio, etc.) serem capturadas como
# advogado do Autor - contaminando o CNA. Agora carrega no import.
_BLACKLIST_BMG = carregar_blacklist()

# Dominios institucionais que NUNCA sao email de advogado privado.
# Aparecem em carimbos, intimacoes, citacoes - falsos positivos.
DOMINIOS_INSTITUCIONAIS = (
    ".jus.br",          # tjmg.jus.br, tjsc.jus.br, etc.
    ".gov.br",          # inss.gov.br, mp.gov.br, etc.
    ".mp.br",
    "bmg.com.br",
    "bancobmg.com.br",
    "lucchesidolabela.com.br",  # nosso proprio escritorio
)

# Palavras que NUNCA sao nome de pessoa (filtra falsos positivos do extrator de nome)
_BLACKLIST_NOME = {
    "ADVOGADO", "ADVOGADA", "ADVOGADOS", "OAB", "DOUTOR", "DOUTORA",
    "PEDE", "DEFERIMENTO", "TERMOS", "DEFERIMENTO", "EXMO", "EXMA",
    "JUIZ", "JUIZA", "EGREGIO", "MERITISSIMO", "MM", "JUSTICA",
    "AUTOR", "AUTORA", "REQUERIDO", "REQUERIDA", "BMG", "BANCO",
    "PROCURADOR", "PROCURADORA", "INSCRITO", "INSCRITA", "INSCRICAO",
    "EXMOS", "ESTADO", "COMARCA", "VARA", "SECAO", "TRIBUNAL",
    "RECEBA", "SUBSCRITOR", "SUBSCRITORES", "SUBSCREVE", "NESTES",
    "ASSINADO", "DIGITALMENTE", "ELETRONICAMENTE",
}

# Limite de advogados que retornamos por PDF (a maioria tem 1-3)
MAX_ADVOGADOS = 5


@dataclass
class Advogado:
    nome: str = ""
    oab: str = ""          # 'OAB/RS 12345' (forma normalizada)
    uf: str = ""           # 'RS'
    email: str = ""
    telefone: str = ""     # fixo (xxxx-xxxx, comeca com 2/3/4)
    celular: str = ""      # movel (9xxxx-xxxx)

    def __bool__(self):
        return bool(self.nome or self.oab or self.email or self.telefone or self.celular)


@dataclass
class RelatorioPdf:
    """Saida da extracao de UM PDF."""
    pdf_path: Path
    texto_chars: int = 0
    ocr_necessario: bool = False
    advogados: list = field(default_factory=list)
    email_parte_autora: str = ""
    erro: str = ""


def _strip_acentos(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))


def _normaliza_oab(uf: str, numero: str, sufixo: str = "") -> str:
    """('SP', '123.456', '') -> 'OAB/SP 123456'."""
    numero_limpo = re.sub(r"[^\d]", "", numero or "")
    uf_norm = (uf or "").upper()
    if not numero_limpo:
        return ""
    base = f"OAB/{uf_norm} {numero_limpo}" if uf_norm else f"OAB {numero_limpo}"
    if sufixo:
        base += f"/{sufixo.upper()}"
    return base


def _classifica_telefone(ddd: str, prefixo: str, sufixo: str) -> tuple:
    """Retorna (telefone_fixo, celular) - so um eh preenchido.
    Celular comeca com 9 (formato 9xxxx-xxxx). Fixo comeca com 2/3/4/5.
    """
    numero_completo = f"({ddd}) {prefixo}-{sufixo}"
    if prefixo.startswith("9") and len(prefixo) == 5:
        return ("", numero_completo)
    if prefixo[0] in "2345" and len(prefixo) == 4:
        return (numero_completo, "")
    # Ambiguo - retorna como fixo (default conservador)
    return (numero_completo, "")


def _extrair_texto(pdf_path: Path) -> tuple:
    """Le o PDF via pdfplumber. Retorna (texto_completo, n_paginas, erro)."""
    if pdfplumber is None:
        return ("", 0, "pdfplumber nao instalado")
    try:
        partes = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            n_paginas = len(pdf.pages)
            for pagina in pdf.pages:
                txt = pagina.extract_text() or ""
                if txt.strip():
                    partes.append(txt)
        return ("\n".join(partes), n_paginas, "")
    except Exception as e:
        return ("", 0, f"{type(e).__name__}: {e}")


def _linhas_uteis(texto: str) -> list:
    """Divide o texto em linhas, ignora linhas vazias."""
    return [l.strip() for l in texto.splitlines() if l.strip()]


def _eh_provavel_nome(linha: str) -> bool:
    """True se a linha parece nome de pessoa.

    Heuristica:
      - 2+ palavras
      - todas palavras com pelo menos 1 letra
      - >=80% letras maiusculas OU Capitalized
      - nao contem digitos
      - nenhuma palavra na blacklist
    """
    s = linha.strip()
    if not s or any(c.isdigit() for c in s):
        return False
    palavras = s.split()
    if len(palavras) < 2 or len(palavras) > 8:
        return False
    palavras_alpha = [p for p in palavras if any(c.isalpha() for c in p)]
    if len(palavras_alpha) != len(palavras):
        return False
    # Filtra blacklist
    palavras_up = [_strip_acentos(p).upper().rstrip(".,;:") for p in palavras]
    if any(p in _BLACKLIST_NOME for p in palavras_up):
        return False
    # Verifica que >=80% das letras sao maiusculas OU formato Capitalized
    letras = [c for c in s if c.isalpha()]
    if not letras:
        return False
    maiusc_ratio = sum(1 for c in letras if c.isupper()) / len(letras)
    # Aceita ALL CAPS (>=0.8) OU Capitalized (cada palavra com 1a letra maiuscula)
    if maiusc_ratio >= 0.7:
        return True
    if all(p[0].isupper() for p in palavras if p and p[0].isalpha()):
        return True
    return False


def _eh_socio_blacklist(nome: str, oab_numero: str) -> bool:
    """True se o advogado eh do Banco/socio LD (blacklist compartilhada).
    Nome mantido por compatibilidade; delega pra core.blacklist."""
    return esta_no_bmg(nome, oab_numero, _BLACKLIST_BMG)


def _eh_email_institucional(email: str) -> bool:
    """True se eh email de TJ/INSS/BMG (nao usar como contato de advogado privado)."""
    em_low = (email or "").lower()
    return any(em_low.endswith(d) or d in em_low for d in DOMINIOS_INSTITUCIONAIS)


def _extrair_advogados(texto: str) -> list:
    """Encontra OAB no texto e tenta associar nome ao redor.
    Filtra socios do Lucchesi Dolabela (parte RE)."""
    linhas = _linhas_uteis(texto)
    achados: list = []
    seen_oabs: set = set()
    blacklist_count = 0

    for i, linha in enumerate(linhas):
        m = RE_OAB.search(linha)
        if not m:
            continue
        uf = (m.group(1) or "").upper()
        numero_raw = m.group(2) or ""
        sufixo = (m.group(3) or "").upper()
        oab_norm = _normaliza_oab(uf, numero_raw, sufixo)
        if not oab_norm or oab_norm in seen_oabs:
            continue
        seen_oabs.add(oab_norm)

        # Procura nome em linhas pertinentes (anterior, mesma, proxima)
        nome = ""
        candidatos = []
        if i - 1 >= 0:
            candidatos.append(linhas[i - 1])
        # Tira a propria substring OAB da linha pra olhar o que sobra
        linha_sem_oab = RE_OAB.sub("", linha).strip(" -.,;:|")
        if linha_sem_oab:
            candidatos.append(linha_sem_oab)
        if i + 1 < len(linhas):
            candidatos.append(linhas[i + 1])

        for c in candidatos:
            if _eh_provavel_nome(c):
                nome = c.strip(" -.,;:|")
                break

        # BLACKLIST: socio do Lucchesi Dolabela -> ignora
        if _eh_socio_blacklist(nome, numero_raw):
            blacklist_count += 1
            continue

        adv = Advogado(nome=nome, oab=oab_norm, uf=uf)
        achados.append(adv)

    return achados[:MAX_ADVOGADOS]


def _atribuir_emails_telefones(texto: str, advogados: list) -> tuple:
    """Atribui e-mails e telefones encontrados aos advogados (best-effort).

    Estrategia simples: emails encontrados na MESMA LINHA da OAB ou
    proximas (janela +/- 3 linhas) ficam com aquele advogado. Sobras
    viram email_parte_autora (se houver UM email isolado).
    """
    linhas = _linhas_uteis(texto)
    # Mapeia: numero_oab_str -> indice no advogados[]
    posicoes_oab = {}
    for adv in advogados:
        for i, linha in enumerate(linhas):
            if adv.oab.split()[-1] in linha and "OAB" in linha.upper():
                posicoes_oab[adv.oab] = i
                break

    emails_atribuidos = set()
    telefones_atribuidos = set()

    # Janela apertada: so linhas imediatamente vizinhas a OAB (idx-1, idx, idx+1).
    # Janelas maiores embaralham email do escritorio com email do advogado
    # quando ambos aparecem no timbre.
    # Pra atribuicao de telefone corrido, faz UMA varredura no texto todo
    # extraindo todos os telefones (com ou sem separador) e suas posicoes.
    # Depois cada advogado pega o mais proximo da sua OAB.
    todos_tels: list = []  # [(pos_inicio, fixo, cel), ...]
    for m in RE_TELEFONE.finditer(texto):
        ddd, prefixo, sufixo = m.group(1), m.group(2), m.group(3)
        fixo, cel = _classifica_telefone(ddd, prefixo, sufixo)
        if fixo or cel:
            todos_tels.append((m.start(), fixo, cel))
    for m in RE_TELEFONE_CORRIDO.finditer(texto):
        d = m.group(1)
        # 11 digitos: DDD(2) + celular(9 + 4 + 4) ou 11 chars com 9 no 3o
        # 10 digitos: DDD(2) + fixo(4 + 4)
        if len(d) == 11:
            ddd, pref, suf = d[:2], d[2:7], d[7:]
        else:  # 10
            ddd, pref, suf = d[:2], d[2:6], d[6:]
        fixo, cel = _classifica_telefone(ddd, pref, suf)
        if fixo or cel:
            todos_tels.append((m.start(), fixo, cel))

    for adv in advogados:
        idx = posicoes_oab.get(adv.oab)
        if idx is None:
            continue
        # Janela apertada pra email (+/-1) - emails do timbre nao se mesclam
        janela_email = linhas[max(0, idx - 1): min(len(linhas), idx + 2)]
        bloco_email = " ".join(janela_email)
        # Janela maior pra telefone (+/-4) - telefone vem em linha separada,
        # tipicamente abaixo do nome/OAB. Menos risco de embaralhar.
        # Subiu de +/-2 pra +/-4 pq muitos PDFs tem o tel 3-4 linhas distante.
        janela_tel = linhas[max(0, idx - 4): min(len(linhas), idx + 5)]
        bloco_tel = " ".join(janela_tel)
        bloco = bloco_email  # legacy nome, mantido pra clareza visual
        # Email - ignora institucionais
        if not adv.email:
            for m in RE_EMAIL.finditer(bloco_email):
                em = m.group(1).lower()
                if em in emails_atribuidos:
                    continue
                if _eh_email_institucional(em):
                    emails_atribuidos.add(em)  # consume, mas nao atribui
                    continue
                adv.email = em
                emails_atribuidos.add(em)
                break
        # Telefone - usa janela maior (+/-2)
        if not adv.telefone and not adv.celular:
            for m in RE_TELEFONE.finditer(bloco_tel):
                ddd, prefixo, sufixo = m.group(1), m.group(2), m.group(3)
                chave = f"{ddd}{prefixo}{sufixo}"
                if chave in telefones_atribuidos:
                    continue
                fixo, cel = _classifica_telefone(ddd, prefixo, sufixo)
                if fixo or cel:
                    adv.telefone = fixo
                    adv.celular = cel
                    telefones_atribuidos.add(chave)
                    break
            # Se ainda nao tem tel, tenta formato com pontos (9.9941.3571)
            if not adv.telefone and not adv.celular:
                for m in RE_TELEFONE_CEL_PONTO.finditer(bloco_tel):
                    ddd, p, s = m.group(1), m.group(2), m.group(3)
                    # Formato com pontos eh SEMPRE celular (9 explicito).
                    # Reconstroi como prefixo "9" + 4 digitos = 5 digitos.
                    prefixo_5 = "9" + p
                    chave = f"{ddd}{prefixo_5}{s}"
                    if chave in telefones_atribuidos:
                        continue
                    fixo, cel = _classifica_telefone(ddd, prefixo_5, s)
                    if fixo or cel:
                        adv.telefone = fixo
                        adv.celular = cel
                        telefones_atribuidos.add(chave)
                        break
            # Se ainda nao tem tel, tenta corrido no bloco
            if not adv.telefone and not adv.celular:
                for m in RE_TELEFONE_CORRIDO.finditer(bloco_tel):
                    d = m.group(1)
                    if len(d) == 11:
                        ddd, pref, suf = d[:2], d[2:7], d[7:]
                    else:
                        ddd, pref, suf = d[:2], d[2:6], d[6:]
                    chave = f"{ddd}{pref}{suf}"
                    if chave in telefones_atribuidos:
                        continue
                    fixo, cel = _classifica_telefone(ddd, pref, suf)
                    if fixo or cel:
                        adv.telefone = fixo
                        adv.celular = cel
                        telefones_atribuidos.add(chave)
                        break

    # FALLBACK GLOBAL pra telefone: se algum advogado ainda nao tem tel,
    # pega o 1o telefone nao-atribuido do PDF inteiro. Tipicamente eh o
    # do timbre/cabecalho do escritorio - serve pra todos os advogados da
    # mesma firma. Preferencia pra celular sobre fixo (whatsapp).
    sem_telefone = [a for a in advogados if not (a.telefone or a.celular)]
    if sem_telefone and todos_tels:
        # Ordena por celular primeiro (preferimos whatsapp)
        candidatos_orfaos = []
        for pos, fixo, cel in todos_tels:
            chave = (fixo or cel).replace(" ", "").replace("(", "").replace(")", "").replace("-", "")
            if chave in telefones_atribuidos:
                continue
            candidatos_orfaos.append((pos, fixo, cel, chave))
        # Celular primeiro, depois fixo
        candidatos_orfaos.sort(key=lambda x: (0 if x[2] else 1, x[0]))
        for adv in sem_telefone:
            if not candidatos_orfaos:
                break
            pos, fixo, cel, chave = candidatos_orfaos.pop(0)
            adv.telefone = fixo
            adv.celular = cel
            telefones_atribuidos.add(chave)

    # Email da parte autora: pega o 1o email NAO institucional E nao atribuido
    email_parte_autora = ""
    for m in RE_EMAIL.finditer(texto):
        em = m.group(1).lower()
        if em in emails_atribuidos:
            continue
        if _eh_email_institucional(em):
            continue
        email_parte_autora = em
        break

    return advogados, email_parte_autora


def extrair_de_pdf(pdf_path: Path) -> RelatorioPdf:
    """Funcao principal: le UM PDF e devolve um RelatorioPdf."""
    pdf_path = Path(pdf_path)
    rel = RelatorioPdf(pdf_path=pdf_path)

    texto, n_pag, erro = _extrair_texto(pdf_path)
    rel.texto_chars = len(texto)
    if erro:
        rel.erro = erro
        return rel

    # Se texto vazio ou muito curto, eh PDF imagem
    if rel.texto_chars < OCR_THRESHOLD_CHARS:
        rel.ocr_necessario = True
        return rel

    # Extrai advogados via regex OAB
    advogados = _extrair_advogados(texto)
    if advogados:
        advogados, email_parte = _atribuir_emails_telefones(texto, advogados)
        rel.advogados = [a for a in advogados if a]
        rel.email_parte_autora = email_parte
    else:
        # Sem OAB: nada extraido, mas texto extraivel - nao precisa de OCR
        rel.advogados = []

    return rel
