"""
Captura dos dados da aba Financeiro do e-Xyon (lancamento de pagamento).

A partir de 06/2026 o Banco parou de mandar a mensagem de "PEDIDO DE
PAGAMENTO"; as informacoes ficam SO na aba Financeiro. Esta modulo extrai
TODOS os campos do painel "Solicitacoes" do lancamento de pagamento MAIS
RECENTE.

Estrategia (sem seletor especifico - o e-Xyon renderiza label + valor em
texto plano):
  1) Click na aba 'Financeiro' (fraFichas).
  2) Polling ate o conteudo aparecer em algum frame.
  3) Divide o texto em lancamentos (cada bloco comeca em "Cod Lancto:").
  4) Parseia cada bloco em pares label->valor.
  5) Escolhe o lancamento de MAIOR "Data de Pagamento" que NAO seja de
     custas nem honorarios (ver _RE_TIPO_IGNORADO); se pular algum,
     marca revisao.

Campos (ordem do painel):
  Cod Lancto | Procedimento | Parte | Natureza do Lancto | Tipo de Lancto |
  Estorno | Forma de Pagamento | Data de Pagamento | Valor | Fator Correcao |
  Valor Total Baixa | Valor Astreinte | Vl. Credito Compensado |
  Valor Dano Material | Valor Dano Moral | Valor Multa | Total de Execucao |
  Honorarios | Data Base | Saldo Atualizado | Data Referencia |
  Fonte Pagadora | Justificativa
"""
import re
import time

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from .frames import _frame_fichas


TIMEOUT_ABA_MS = 8_000
# Teto de espera pro painel estabilizar. Dobrado de 10s pra 20s (09/2026):
# o polling ja sai MAIS CEDO assim que o texto para de mudar (ver
# ESTABILIZACAO_POLLS abaixo), entao esse teto so pesa no caso lento -
# nao atrasa o caminho comum. Da mais margem pro round-trip do(s)
# lancamento(s) pendente(s) de Aprovacao de Alcada, que carrega separado
# e mais devagar que o resto da tabela.
TIMEOUT_RENDER_MS = 20_000
POLL_INTERVAL_MS = 500
# Leituras CONSECUTIVAS com o MESMO tamanho de texto antes de considerar
# o painel "pronto". O painel as vezes renderiza os lancamentos normais
# primeiro e busca o(s) pendente(s) de Aprovacao de Alcada num round-trip
# separado, um pouco mais lento - parar no 1o match perdia esses
# lancamentos mais recentes (caso real: CIV0761635, 09/2026).
ESTABILIZACAO_POLLS = 2

# (chave_interna, rotulo_no_painel). A chave vira coluna na planilha.
# O rotulo eh casado como "<rotulo> :" (com espacos/tabs tolerados). Rotulos
# que sao prefixo de outros (ex.: "Valor" vs "Valor Total Baixa") funcionam
# porque exigimos os dois-pontos logo apos - "Valor Total" nao tem ":" depois
# de "Valor".
CAMPOS = [
    ("cod_lancto",            "Cod Lancto"),
    ("procedimento",          "Procedimento"),
    ("parte",                 "Parte"),
    ("natureza_lancto",       "Natureza do Lancto"),
    ("tipo_lancto",           "Tipo de Lancto"),
    ("estorno",               "Estorno"),
    ("forma_pagamento",       "Forma de Pagamento"),
    ("data_pagamento",        "Data de Pagamento"),
    ("valor",                 "Valor"),
    ("fator_correcao",        "Fator Correção"),
    ("valor_total_baixa",     "Valor Total Baixa"),
    ("valor_astreinte",       "Valor Astreinte"),
    ("vl_credito_compensado", "Vl. Crédito Compensado"),
    ("valor_dano_material",   "Valor Dano Material"),
    ("valor_dano_moral",      "Valor Dano Moral"),
    ("valor_multa",           "Valor Multa"),
    ("total_execucao",        "Total de Execução"),
    ("honorarios",            "Honorários"),
    ("data_base",             "Data Base"),
    ("saldo_atualizado",      "Saldo Atualizado"),
    ("data_referencia",       "Data Referência"),
    ("fonte_pagadora",        "Fonte Pagadora"),
    ("justificativa",         "Justificativa"),
]

# Regex por campo: rotulo + espacos/tabs + ":". Pre-compilado.
_PAT_CAMPO = [
    (chave, re.compile(re.escape(rotulo) + r"\s*:", re.IGNORECASE))
    for chave, rotulo in CAMPOS
]

# Chaves extras (nao sao campos do painel) que o extrator agrega ao dict de
# retorno pra sinalizar revisao do operador. Viram colunas na planilha.
CHAVE_REVISAR = "revisar"
CHAVE_MOTIVO_REVISAO = "motivo_revisao"

# Chave extra: valor BRUTO da coluna Estagio da tabela de Solicitacoes
# (ex.: "Finalizado", "Aprovação Alçada", "Aguardando", ...). Vira coluna
# propria na planilha de saida - o operador ve exatamente o que esta no
# e-Xyon, nao so um booleano.
CHAVE_ESTAGIO = "estagio"

# Sinal "Solicitacao finalizada" - quando o Estagio e Finalizado/Finalizada,
# o pagamento ja foi encerrado e NAO cabe mais acordo. O negociador precisa
# conferir manualmente porque nao ha o que negociar. Os campos abaixo viram
# colunas na planilha de saida.
CHAVE_NAO_EXIGIVEL = "nao_exigivel"
CHAVE_MOTIVO_NAO_EXIGIVEL = "motivo_nao_exigivel"

# A coluna Estagio NAO tem rotulo "Estagio:" no innerText - o valor aparece
# como texto solto, numa linha propria, perto do fim de cada bloco (mesma
# regiao onde ficam os contadores "(N)" de Docs/Baixa). Duas camadas:
#
#   1) Vocabulario CONHECIDO (seguro): valores ja confirmados em producao.
#      Cresca esta lista conforme aparecerem novos - e so adicionar uma
#      string. So estes valores acionam CHAVE_NAO_EXIGIVEL.
#   2) Fallback (melhor esforco): se nada do vocabulario bateu, procura
#      uma UNICA linha curta e "limpa" (so letras/acento/espaco, sem
#      digito nem pontuacao de frase) na CAUDA do bloco (apos o ultimo
#      rotulo casado). So usa se houver EXATAMENTE uma candidata - se
#      houver 0 ou mais de 1, fica em branco (nao arrisca capturar
#      pedaco de Justificativa). NUNCA aciona CHAVE_NAO_EXIGIVEL sozinho.
ESTAGIOS_CONHECIDOS = [
    "Finalizado",
    "Finalizada",
    "Aprovação Alçada",
]

_RE_ESTAGIO_CONHECIDO = re.compile(
    r"(?:^|\n)[ \t]*(" + "|".join(re.escape(v) for v in ESTAGIOS_CONHECIDOS)
    + r")[ \t]*(?:\n|$)",
    re.IGNORECASE,
)

_RE_LINHA_CURTA = re.compile(
    r"(?:^|\n)[ \t]*([A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ \t\-]{1,38}[A-Za-zÀ-ÖØ-öø-ÿ])[ \t]*(?:\n|$)"
)


def _extrair_estagio(bloco: str, ocorrencias: list, res: dict) -> str:
    """Extrai o texto BRUTO da coluna Estagio de um bloco de lancamento.
    Retorna '' se nao identificou com seguranca (ver comentario acima).

    `res`: dict JA preenchido com os CAMPOS (apos o strip de rodape em
    'justificativa'). Necessario pro fallback: a cauda do bloco comeca
    logo apos o ULTIMO rotulo casado, mas essa cauda tambem contem o
    VALOR desse campo (ex.: a propria Justificativa) - se nao descontar
    esse valor primeiro, uma Justificativa curta e sem pontuacao (ex.:
    'Pedido em analise') tambem parece uma 'linha limpa' e vira uma 2a
    candidata, fazendo o fallback desistir por ambiguidade sempre que a
    Justificativa for curta. Descontar o valor ja resolvido evita isso.
    """
    m = _RE_ESTAGIO_CONHECIDO.search(bloco)
    if m:
        achado_lower = m.group(1).strip().lower()
        for v in ESTAGIOS_CONHECIDOS:
            if v.lower() == achado_lower:
                return v
        return m.group(1).strip()

    if not ocorrencias:
        return ""
    ultima_chave = ocorrencias[-1][2]
    resto = bloco[ocorrencias[-1][1]:]
    valor_ja_atribuido = res.get(ultima_chave, "")
    if valor_ja_atribuido:
        idx = resto.find(valor_ja_atribuido)
        if idx != -1:
            resto = resto[idx + len(valor_ja_atribuido):]
    resto = re.sub(r"\(\s*\d+\s*\)", "", resto)  # contadores Docs/Baixa
    candidatos = [c.strip() for c in _RE_LINHA_CURTA.findall(resto) if c.strip()]
    return candidatos[0] if len(candidatos) == 1 else ""

# O alvo da extracao e a solicitacao de pagamento de CONDENACAO. O painel,
# porem, mistura lancamentos que nao interessam:
#   - custas     (ex.: "CUSTA FINAIS", "CUSTAS DIVERSAS E TAXAS JUDICIAIS")
#   - honorarios (ex.: "HONORARIOS FINAIS", "HONORARIOS INICIAIS - DEMANDA
#                 MIGRADA", "HONORARIOS PARCELA 21")
# Quando o lancamento mais recente e de um desses tipos, o robo desce pro
# lancamento anterior e marca a linha pra revisao do operador (coluna
# Revisar = SIM). Pra ignorar outros tipos, basta acrescentar ao regex.
_RE_TIPO_IGNORADO = re.compile(
    r"\b(CUSTAS?|HONOR[ÁA]RIOS?)\b", re.IGNORECASE
)

# Corta a Justificativa (ultimo campo) no rodape do painel: "(1)", "(0)",
# "Aprovação Alçada".
_RE_RODAPE_JUST = re.compile(r"\(\s*\d+\s*\)|Aprova[çc][aã]o\s+Al[çc]ada", re.I)


def abrir_aba_financeiro(page: Page, civ: str = "") -> bool:
    """Click na aba 'Financeiro' em fraFichas. NAO espera o conteudo (isso
    fica no extrair_financeiro, que faz polling)."""
    try:
        fra = _frame_fichas(page)
        fra.get_by_text("Financeiro", exact=True).first.click(timeout=TIMEOUT_ABA_MS)
    except PlaywrightTimeout:
        print(f"  [{civ}] timeout clicando aba Financeiro em fraFichas")
        return False
    except Exception as e:
        print(f"  [{civ}] erro clicando aba Financeiro: {type(e).__name__}: {e}")
        return False
    return True


def _ler_textos_dos_frames(page: Page) -> list:
    out = []
    for frame in page.frames:
        try:
            txt = frame.evaluate("() => (document.body && document.body.innerText) || ''")
            out.append((frame.name or "(sem nome)", frame.url, txt or ""))
        except Exception:
            out.append((frame.name or "(sem nome)", frame.url, ""))
    return out


def _limpar_valor(v: str) -> str:
    """Colapsa tabs/espacos/quebras num espaco unico e tira as bordas."""
    return re.sub(r"\s+", " ", (v or "").replace("\xa0", " ")).strip()


def parse_lancamento(bloco: str) -> dict:
    """Parseia UM bloco de lancamento em {chave: valor} pra todos os CAMPOS.

    Acha a posicao de cada '<rotulo>:' e usa como valor o texto ate o
    proximo rotulo (em ordem de posicao). Isso segue a ordem de leitura do
    painel (2 colunas viram sequencia no innerText).
    """
    res = {chave: "" for chave, _ in CAMPOS}

    ocorrencias = []  # (pos_inicio, pos_fim_do_rotulo, chave)
    for chave, pat in _PAT_CAMPO:
        m = pat.search(bloco)
        if m:
            ocorrencias.append((m.start(), m.end(), chave))
    if not ocorrencias:
        return res
    ocorrencias.sort(key=lambda x: x[0])

    for i, (ini, fim, chave) in enumerate(ocorrencias):
        prox = ocorrencias[i + 1][0] if i + 1 < len(ocorrencias) else len(bloco)
        res[chave] = _limpar_valor(bloco[fim:prox])

    # Justificativa: corta o rodape do painel se veio junto.
    if res.get("justificativa"):
        res["justificativa"] = _RE_RODAPE_JUST.split(res["justificativa"])[0].strip()

    # Estagio: valor bruto (Finalizado, Aprovação Alçada, ...) vira coluna
    # propria. So o vocabulario CONHECIDO (Finalizado/Finalizada) aciona
    # Nao_Exigivel - o fallback de melhor esforco nao decide nada sozinho.
    estagio = _extrair_estagio(bloco, ocorrencias, res)
    res[CHAVE_ESTAGIO] = estagio
    if estagio.strip().lower() in ("finalizado", "finalizada"):
        res[CHAVE_NAO_EXIGIVEL] = "SIM"
        res[CHAVE_MOTIVO_NAO_EXIGIVEL] = (
            "Solicitacao FINALIZADA (coluna Estagio) - pagamento ja "
            "encerrado, nao cabe mais acordo. Conferir manualmente."
        )
    else:
        res[CHAVE_NAO_EXIGIVEL] = ""
        res[CHAVE_MOTIVO_NAO_EXIGIVEL] = ""

    return res


def _split_lancamentos(texto: str) -> list:
    """Divide o texto do painel em blocos de lancamento (cada um comeca em
    'Cod Lancto:'). Se nao achar o marcador, devolve o texto inteiro como
    bloco unico (fallback)."""
    partes = re.split(r"(?=Cod\s+Lancto\s*:)", texto)
    blocos = [p for p in partes if re.search(r"Cod\s+Lancto\s*:", p)]
    return blocos or ([texto] if texto.strip() else [])


def _chave_data(data_str: str) -> tuple:
    """DD/MM/AAAA -> (AAAA, MM, DD) pra ordenar. (0,0,0) se invalido."""
    m = re.search(r"([0-3]?\d)/([01]?\d)/(\d{4})", data_str or "")
    if not m:
        return (0, 0, 0)
    d, mth, y = m.groups()
    return (int(y), int(mth), int(d))


def _eh_lancamento_ignorado(lanc: dict) -> bool:
    """True se o lancamento e de custas ou honorarios (nao e condenacao)."""
    return bool(_RE_TIPO_IGNORADO.search(lanc.get("tipo_lancto", "") or ""))


def _descrever(lanc: dict) -> str:
    tipo = lanc.get("tipo_lancto") or "(sem tipo)"
    data = lanc.get("data_pagamento") or "(sem data)"
    estagio = lanc.get(CHAVE_ESTAGIO) or ""
    sufixo = f" [Estagio: {estagio}]" if estagio else ""
    return f"{tipo} em {data}{sufixo}"


def _escolher_lancamento(lancamentos: list, civ: str = "") -> dict:
    """Escolhe o lancamento alvo entre os lancamentos JA ordenados do mais
    recente pro mais antigo, e agrega as chaves de revisao.

    Regra: pula os lancamentos de custas/honorarios e usa o proximo
    (pagamento de condenacao). Sempre que algum lancamento for pulado -
    ou quando TODOS forem ignoraveis - marca revisar=SIM pro operador.
    """
    descartados = []
    for lanc in lancamentos:
        if _eh_lancamento_ignorado(lanc):
            descartados.append(lanc)
            continue

        escolhido = dict(lanc)
        if descartados:
            desc = "; ".join(_descrever(d) for d in descartados)
            print(f"  [{civ}] aba Financeiro: {len(descartados)} lancamento(s) "
                  f"de custas/honorarios ignorado(s) ({desc}); "
                  f"usando '{_descrever(lanc)}'")
            escolhido[CHAVE_REVISAR] = "SIM"
            escolhido[CHAVE_MOTIVO_REVISAO] = (
                f"Lancamento mais recente nao e de condenacao ({desc}) - "
                f"usado o anterior: {_descrever(lanc)}"
            )
        else:
            escolhido[CHAVE_REVISAR] = ""
            escolhido[CHAVE_MOTIVO_REVISAO] = ""
        return escolhido

    # Nenhum elegivel: so custas/honorarios. Devolve o mais recente mesmo
    # assim (o operador decide) e sinaliza.
    escolhido = dict(lancamentos[0])
    desc = "; ".join(_descrever(d) for d in lancamentos)
    print(f"  [{civ}] aba Financeiro: TODOS os {len(lancamentos)} lancamento(s) "
          f"sao de custas/honorarios ({desc}) - marcado pra revisao")
    escolhido[CHAVE_REVISAR] = "SIM"
    escolhido[CHAVE_MOTIVO_REVISAO] = (
        f"Nenhum pagamento de condenacao - so custas/honorarios ({desc})"
    )
    return escolhido


def extrair_financeiro(page: Page, civ: str = "") -> dict:
    """Le o painel da aba Financeiro e retorna {chave: valor} de TODOS os
    campos do lancamento de pagamento MAIS RECENTE. Dict vazio ({}) se nao
    achou nenhum lancamento.

    Espera o painel ESTABILIZAR (ESTABILIZACAO_POLLS leituras seguidas com
    o mesmo tamanho de texto), nao so "aparecer". O e-Xyon pode renderizar
    os lancamentos normais primeiro e buscar os pendentes de Aprovacao de
    Alcada num round-trip a parte, um pouco mais lento - parar no 1o match
    perdia esses lancamentos mais recentes."""
    deadline = time.monotonic() + (TIMEOUT_RENDER_MS / 1000)
    melhor_texto = ""
    tentativas = 0
    tam_anterior = None
    polls_estaveis = 0

    while time.monotonic() < deadline:
        tentativas += 1
        candidatos = []
        for _, _, txt in _ler_textos_dos_frames(page):
            if not txt:
                continue
            if ("Cod Lancto" in txt or "Data de Pagamento" in txt
                    or "Saldo Atualizado" in txt):
                candidatos.append(txt)

        if candidatos:
            texto_atual = max(candidatos, key=len)
            if len(texto_atual) > len(melhor_texto):
                melhor_texto = texto_atual
            if tam_anterior is not None and len(texto_atual) == tam_anterior:
                polls_estaveis += 1
                if polls_estaveis >= ESTABILIZACAO_POLLS:
                    break
            else:
                polls_estaveis = 0
            tam_anterior = len(texto_atual)

        time.sleep(POLL_INTERVAL_MS / 1000)

    if not melhor_texto:
        print(f"  [{civ}] aba Financeiro: nada encontrado apos {tentativas} tentativas "
              f"({TIMEOUT_RENDER_MS}ms). DIAGNOSTICO - frames:")
        for nome, url, txt in _ler_textos_dos_frames(page):
            url_curta = url[-70:] if len(url) > 70 else url
            snip = (txt[:120].replace("\n", " | ") + "...") if len(txt) > 120 else txt.replace("\n", " | ")
            print(f"      frame='{nome}' url=...{url_curta} text={snip!r}")
        return {}

    texto = melhor_texto
    blocos = _split_lancamentos(texto)

    lancamentos = [parse_lancamento(b) for b in blocos]
    lancamentos = [l for l in lancamentos if any(l.values())]
    if not lancamentos:
        print(f"  [{civ}] aba Financeiro: painel encontrado mas nenhum campo parseado")
        return {}

    lancamentos.sort(key=lambda l: _chave_data(l.get("data_pagamento", "")), reverse=True)

    if len(lancamentos) > 1:
        print(f"  [{civ}] aba Financeiro: {len(lancamentos)} lancamentos; "
              f"mais recente e de data {lancamentos[0].get('data_pagamento') or '(sem data)'}")

    return _escolher_lancamento(lancamentos, civ)


def capturar_financeiro(page: Page, civ: str = "") -> dict:
    """abre + extrai. Retorna dict de campos ou {} se falhar. Nao levanta."""
    if not abrir_aba_financeiro(page, civ):
        return {}
    return extrair_financeiro(page, civ)
