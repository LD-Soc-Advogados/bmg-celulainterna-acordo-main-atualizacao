"""
Tarefa 5 - Enriquecer advogados com CONTATO via CNA (cna.oab.org.br).

O Cadastro Nacional dos Advogados (CNA) expoe, na ficha de cada inscricao,
os campos "Telefone Profissional" e "Endereco Profissional" - QUANDO o
advogado os preencheu (e opcional; muitos nao tem). Tambem traz Subsecao
(cidade) e Situacao (REGULAR/SUSPENSO/etc.).

Esses dados completam a Tarefa 4 (DJEN), que da nome + OAB atual: aqui
pegamos o contato pra equipe procurar o advogado e propor acordo.

LIMITACOES TECNICAS (descobertas em 06/2026):
  - O site e uma SPA Angular protegida por reCAPTCHA v3 (+ v2 fallback).
    A API (/advogado/search) EXIGE token do reCAPTCHA (testado: responde
    400 "Recaptcha token is missing"); NAO da pra consultar via HTTP puro.
    Precisa de navegador real que gere o token.
  - Usamos um navegador PROPRIO (Chromium do Playwright, perfil temporario
    e limpo), NAO o Edge dedicado. O Edge dedicado vive cheio de automacao
    (Zeev, e-Xyon, CDP) e ganha nota baixa no reCAPTCHA v3 -> cai no desafio
    de imagens (v2). Um navegador limpo, em volume moderado, mantem nota
    melhor e passa silencioso na maioria das vezes.
  - A busca so retorna nome+inscricao; telefone/e-mail/endereco aparecem so
    na FICHA DE DETALHE (ao clicar no resultado). Raspamos o modal.

NAO precisa do Edge dedicado nem de login - o CNA e publico e o navegador
e aberto pela propria Tarefa 5.
"""
import re
import time
import unicodedata

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from core.browser import aplicar_stealth_ao_contexto, aplicar_stealth_ao_page
from core.captcha import desafio_visivel, aguardar_humano


NOME_FLUXO = "Consultar contato CNA"

CNA_URL = "https://cna.oab.org.br/"

TIMEOUT_SPA_MS = 15_000     # SPA + recaptcha carregarem
TIMEOUT_BUSCA_MS = 12_000   # resultado da busca aparecer
TIMEOUT_FICHA_MS = 10_000   # modal de detalhe abrir

# Modal de rate-limit do CNA. Texto exato visto em prod (08/2026):
#   "O limite de consultas permitido em um determinado periodo foi
#    excedido. Aguarde alguns instantes e tente novamente."
# Casamos parcial (case-insensitive) pra sobreviver a mudancas de
# pontuacao/quebras. O modal bloqueia a busca ate ser dispensado.
_TEXTO_RATE_LIMIT = "limite de consultas permitido"

# Codigo sentinela que a funcao devolve pro main quando o rate-limit
# persistiu depois da tentativa local. O main reinicia o navegador
# trocando de canal (msedge <-> chromium) na proxima passada.
ERRO_LIMITE_CNA = "LIMITE_CNA"

# ---------------------------------------------------------------------------
# Desafio "Confirme que voce nao e um robo" (reCAPTCHA v2)
# ---------------------------------------------------------------------------
# Quando a nota do reCAPTCHA v3 cai (volume alto, navegador "sujo" de
# automacao, IP do escritorio muito usado), o CNA passa a exigir o desafio
# v2 - aquele modal com o checkbox "Nao sou um robo".
#
# NAO tentamos resolver/burlar esse desafio: ele existe justamente pra ser
# resolvido por uma PESSOA, e automatizar isso violaria os termos do CNA
# (risco de bloquear o acesso do escritorio ao site). A saida e parar,
# CHAMAR O OPERADOR pra resolver na tela do proprio navegador que ja esta
# aberto, e retomar de onde parou.
#
# Titulo do modal renderizado pela SPA do CNA (o checkbox em si mora num
# iframe do Google, fora do nosso alcance - por isso casamos o titulo).
_TEXTO_CAPTCHA = "não é um robô"

# Sentinela devolvido quando o operador NAO resolveu dentro do tempo.
ERRO_CAPTCHA_CNA = "CAPTCHA_CNA"

ESPERA_CAPTCHA_S = 300      # 5 min esperando a pessoa resolver


def _captcha_visivel(page) -> bool:
    """True se o desafio 'Confirme que voce nao e um robo' esta na tela."""
    return desafio_visivel(page, _TEXTO_CAPTCHA)


def aguardar_captcha_resolvido(page, contexto: str = "",
                               timeout_s: float = ESPERA_CAPTCHA_S) -> bool:
    """Se o desafio do reCAPTCHA estiver na tela, PAUSA e espera uma pessoa
    resolver no navegador (que ja esta aberto e visivel). Wrapper fino
    sobre core.captcha.aguardar_humano com os textos/tempos do CNA."""
    return aguardar_humano(page, _TEXTO_CAPTCHA, contexto=contexto,
                           nome_site="CNA", timeout_s=timeout_s)


def _tratar_captcha(page, contexto: str, esperar_humano: bool) -> bool:
    """Decide o que fazer quando o desafio aparece. True = pode seguir.

    ESCALADA (do menos pro mais invasivo):
      1. 1a passada (esperar_humano=False): nao incomoda ninguem. Devolve
         False na hora pro main fechar o navegador e reabrir no OUTRO
         canal - perfil e reputacao de reCAPTCHA proprios, entao na maioria
         das vezes o desafio nem reaparece.
      2. 2a passada (esperar_humano=True): o desafio voltou mesmo apos a
         troca de navegador. Ai sim para e chama o operador pra resolver
         na tela.
    """
    if not esperar_humano:
        print(f"  [CNA] desafio 'nao sou um robo' em {contexto} - "
              f"vou trocar de navegador e tentar de novo antes de "
              f"chamar alguem.")
        return False
    return aguardar_captcha_resolvido(page, contexto)


def _detectar_e_dismissar_limite(page) -> bool:
    """Se o modal 'Atencao! O limite de consultas...' esta visivel, clica
    OK e retorna True. Senao, retorna False sem tocar em nada.

    Enquanto o modal esta aberto a SPA fica bloqueada - qualquer busca
    subsequente da timeout aparente. Detectar e dispensar antes de assumir
    'timeout aguardando resultado' evita marcar como erro real algo que
    era so rate-limit."""
    try:
        loc = page.get_by_text(_TEXTO_RATE_LIMIT, exact=False)
        if loc.count() == 0 or not loc.first.is_visible():
            return False
        try:
            page.get_by_role("button", name="OK").first.click(timeout=3000)
        except Exception:
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass
        time.sleep(0.6)
        return True
    except Exception:
        return False

# Rotulos da ficha (modal). Sao DISTINTOS do rodape institucional
# ("Endereco" / "Telefones" do Conselho Federal), entao casamos exato.
_LABELS = {
    "telefone": "Telefone Profissional",
    "email": "E-mail Profissional",
    "endereco": "Endereço Profissional",
    "subsecao": "Subseção",
    "situacao": "Situação",
    "nome": "Nome",
    "tipo": "Tipo de Inscrição",
}


def _so_digitos(s) -> str:
    return re.sub(r"[^\d]", "", str(s or ""))


def abrir_pagina_cna(context) -> Page:
    """Abre uma nova aba no Edge dedicado e navega pro CNA.
    Retorna a Page pronta (form de busca carregado)."""
    aplicar_stealth_ao_contexto(context)
    page = context.new_page()
    aplicar_stealth_ao_page(page)
    page.goto(CNA_URL, timeout=60_000, wait_until="domcontentloaded")
    # Espera o form da SPA renderizar (campo registration aparece).
    try:
        page.locator('input[name="registration"]').wait_for(
            state="visible", timeout=TIMEOUT_SPA_MS
        )
    except PlaywrightTimeout:
        time.sleep(3)  # fallback: da um tempo extra mesmo sem o seletor
    return page


def _parse_ficha(texto: str) -> dict:
    """Extrai campos da ficha a partir do innerText do modal.

    Estrutura renderizada (cada rotulo numa linha, valor na linha
    seguinte):
        Inscrição
        124826
        Seccional
        MG
        Subseção
        BELO HORIZONTE
        Telefone Profissional
        (31) 30248120
        Endereço Profissional
        AVENIDA ...
        Situação
        SITUAÇÃO REGULAR

    Campos ausentes (advogado sem telefone/endereco) ficam vazios.
    """
    linhas = [l.strip() for l in (texto or "").splitlines()]
    linhas = [l for l in linhas if l]

    # Conjunto de TODOS os rotulos conhecidos, pra saber onde um bloco termina.
    _labels_lower = {v.lower() for v in _LABELS.values()}

    def _valor_apos(label: str) -> str:
        for i, l in enumerate(linhas):
            if l.lower() == label.lower():
                if i + 1 < len(linhas):
                    return linhas[i + 1].strip()
                return ""
        return ""

    def _bloco_apos(label: str) -> list:
        # Retorna TODAS as linhas entre o rotulo e o proximo rotulo conhecido.
        # Usado pra campos que podem ter varios valores (fixo + celular).
        out = []
        capturando = False
        for l in linhas:
            if l.lower() == label.lower():
                capturando = True
                continue
            if capturando:
                if l.lower() in _labels_lower:
                    break
                out.append(l.strip())
        return out

    # Telefones: TODOS os que parecem telefone (fixo E celular), nao so o 1o.
    tels = []
    for l in _bloco_apos(_LABELS["telefone"]):
        if re.search(r"\d{4}", l) and re.match(r"^\(?\d{2}", l):
            if l not in ("(61) 2193-9600", "(61) 2193-9600 | (61) 99944-5541"):
                tels.append(l)
    # dedup preservando ordem
    tels = list(dict.fromkeys(tels))
    telefone = " / ".join(tels)

    # E-mails: TODOS os que tem "@".
    emails = [l for l in _bloco_apos(_LABELS["email"]) if "@" in l]
    emails = list(dict.fromkeys(emails))
    email = " / ".join(emails)

    endereco = _valor_apos(_LABELS["endereco"])
    subsecao = _valor_apos(_LABELS["subsecao"])
    situacao = _valor_apos(_LABELS["situacao"])

    # Defesa: endereco do rodape institucional (Conselho Federal) descartado.
    if endereco and ("CONSELHO FEDERAL" in endereco.upper()
                     or "SAUS QUADRA" in endereco.upper()):
        endereco = ""

    return {
        "telefone": telefone,
        "email": email,
        "endereco": endereco,
        "subsecao": subsecao,
        "situacao": situacao,
    }


def consultar_por_inscricao(page: Page, inscricao: str, uf: str,
                            nome_hint: str = "",
                            esperar_humano_no_captcha: bool = True) -> dict:
    """Consulta UMA inscricao+UF no CNA e raspa a ficha de detalhe.

    Retorna dict {telefone, endereco, subsecao, situacao, ok, erro}.
    ok=False + erro preenchido se algo falhou (sem resultado, timeout...).

    esperar_humano_no_captcha: o que fazer quando cair o desafio
      "nao sou um robo".
        False (1a passada) - devolve ERRO_CAPTCHA_CNA na hora, SEM
          incomodar ninguem. O main fecha o navegador, reabre no outro
          canal (perfil e reputacao proprios) e refaz - muitas vezes o
          desafio nem aparece de novo.
        True (2a passada, ja no outro navegador) - o desafio voltou
          mesmo depois da troca, entao ai sim para e chama o operador.
    """
    insc = _so_digitos(inscricao)
    uf = (uf or "").strip().upper()
    vazio = {"telefone": "", "email": "", "endereco": "", "subsecao": "",
             "situacao": "", "ok": False, "erro": ""}

    if not insc or not uf:
        vazio["erro"] = f"inscricao/uf invalidos (insc={inscricao!r} uf={uf!r})"
        return vazio

    # Reseta pra um formulario LIMPO antes de cada consulta. Sem isso, o
    # modal/resultado da consulta anterior pode bloquear o campo de busca
    # (visto em lote: a 1a falhava com "timeout preenchendo" depois de
    # algumas consultas OK). Recarregar garante estado previsivel.
    try:
        campo = page.locator('input[name="registration"]')
        # Se o form nao esta pronto (modal aberto, SPA em transicao),
        # recarrega a pagina.
        if campo.count() == 0 or not campo.first.is_visible():
            page.goto(CNA_URL, timeout=60_000, wait_until="domcontentloaded")
            campo.first.wait_for(state="visible", timeout=TIMEOUT_SPA_MS)
    except Exception:
        try:
            page.goto(CNA_URL, timeout=60_000, wait_until="domcontentloaded")
            page.locator('input[name="registration"]').first.wait_for(
                state="visible", timeout=TIMEOUT_SPA_MS)
        except Exception:
            pass

    try:
        # 1) Preenche o numero de inscricao
        campo = page.locator('input[name="registration"]')
        campo.click(timeout=TIMEOUT_BUSCA_MS)
        campo.fill("")
        campo.press_sequentially(insc, delay=60)

        # Limpa o campo nome (pode ter resquicio de consulta anterior)
        try:
            page.locator('input[name="name"]').fill("")
        except Exception:
            pass

        # 2) Seleciona a UF (seccional)
        page.locator('select[name="sectional"]').select_option(uf)
        time.sleep(0.4)

        # 3) Pesquisar
        page.get_by_role("button", name="Pesquisar").click(timeout=TIMEOUT_BUSCA_MS)
    except PlaywrightTimeout:
        # Pode ser o desafio "nao sou um robo" bloqueando o form.
        if _captcha_visivel(page):
            if not _tratar_captcha(page, f"insc {insc}/{uf}",
                                   esperar_humano_no_captcha):
                vazio["erro"] = ERRO_CAPTCHA_CNA
                return vazio
            try:
                page.get_by_role("button", name="Pesquisar").click(
                    timeout=TIMEOUT_BUSCA_MS)
            except Exception:
                vazio["erro"] = "timeout preenchendo/buscando (pos-captcha)"
                return vazio
        else:
            vazio["erro"] = "timeout preenchendo/buscando"
            return vazio
    except Exception as e:
        vazio["erro"] = f"erro na busca: {type(e).__name__}: {e}"
        return vazio

    # 3a-pre) Desafio logo apos o click em Pesquisar (caso mais comum).
    if _captcha_visivel(page):
        if not _tratar_captcha(page, f"insc {insc}/{uf}",
                               esperar_humano_no_captcha):
            vazio["erro"] = ERRO_CAPTCHA_CNA
            return vazio
        try:
            page.get_by_role("button", name="Pesquisar").click(
                timeout=TIMEOUT_BUSCA_MS)
        except Exception:
            pass

    # 3a) Rate-limit imediato: as vezes o modal aparece antes mesmo do
    #     wait_for do resultado. Tenta 1x local (clica OK + espera + refaz
    #     Pesquisar); se persistir, devolve LIMITE_CNA pro main trocar
    #     de canal na proxima passada.
    if _detectar_e_dismissar_limite(page):
        print(f"  [CNA] rate-limit no click 'Pesquisar' (insc {insc}/{uf}) - "
              f"OK + 30s + refazer...")
        time.sleep(30.0)
        try:
            page.get_by_role("button", name="Pesquisar").click(timeout=TIMEOUT_BUSCA_MS)
        except Exception:
            pass
        if _detectar_e_dismissar_limite(page):
            vazio["erro"] = ERRO_LIMITE_CNA
            return vazio

    # 4) Aguarda o resultado. A busca por inscricao+UF retorna 1 registro.
    #    Casamos pela INSCRICAO (exata - sem risco de acento no nome) e,
    #    como reforco, tentamos o nome do DJEN.
    try:
        page.get_by_text(insc, exact=False).first.wait_for(
            state="visible", timeout=TIMEOUT_BUSCA_MS
        )
    except PlaywrightTimeout:
        # Rate-limit tardio: o modal aparece 1-3s depois do click e faz
        # o wait_for cair em timeout. Checa antes de concluir "timeout".
        if _detectar_e_dismissar_limite(page):
            print(f"  [CNA] rate-limit apos wait (insc {insc}/{uf}) - "
                  f"OK + 30s + refazer...")
            time.sleep(30.0)
            try:
                page.get_by_role("button", name="Pesquisar").click(timeout=TIMEOUT_BUSCA_MS)
                page.get_by_text(insc, exact=False).first.wait_for(
                    state="visible", timeout=TIMEOUT_BUSCA_MS
                )
            except Exception:
                if _detectar_e_dismissar_limite(page):
                    vazio["erro"] = ERRO_LIMITE_CNA
                    return vazio
                vazio["erro"] = "timeout aguardando resultado (pos-rate-limit)"
                return vazio
        elif _captcha_visivel(page):
            # Desafio apareceu depois do click e travou o resultado.
            if not _tratar_captcha(page, f"insc {insc}/{uf}",
                                   esperar_humano_no_captcha):
                vazio["erro"] = ERRO_CAPTCHA_CNA
                return vazio
            try:
                page.get_by_role("button", name="Pesquisar").click(
                    timeout=TIMEOUT_BUSCA_MS)
                page.get_by_text(insc, exact=False).first.wait_for(
                    state="visible", timeout=TIMEOUT_BUSCA_MS
                )
            except Exception:
                vazio["erro"] = "timeout aguardando resultado (pos-captcha)"
                return vazio
        else:
            corpo = ""
            try:
                corpo = page.evaluate("() => document.body.innerText")
            except Exception:
                pass
            if "Nenhum resultado" in corpo:
                vazio["erro"] = "sem resultado no CNA"
            else:
                vazio["erro"] = "timeout aguardando resultado"
            return vazio

    # 5) Clica no card pra abrir a ficha. Tenta: nome (se hint) -> inscricao
    #    -> primeiro card generico. Para no primeiro que funcionar.
    clicou = False
    candidatos = []
    if nome_hint.strip():
        candidatos.append(nome_hint.strip())
    candidatos.append(insc)
    for alvo in candidatos:
        try:
            loc = page.get_by_text(alvo, exact=False).first
            if loc.count() > 0:
                loc.click(timeout=5000)
                clicou = True
                break
        except Exception:
            continue
    if not clicou:
        vazio["erro"] = "achei resultado mas nao consegui clicar no card"
        return vazio

    # 6) Espera a ficha (modal) renderizar - aguarda aparecer "Situação"
    #    ou "Tipo de Inscrição" (sempre presentes na ficha).
    try:
        page.get_by_text(_LABELS["situacao"], exact=False).first.wait_for(
            state="visible", timeout=TIMEOUT_FICHA_MS
        )
    except PlaywrightTimeout:
        time.sleep(1.5)  # tenta raspar mesmo assim

    try:
        texto = page.evaluate("() => document.body.innerText")
    except Exception as e:
        vazio["erro"] = f"erro lendo ficha: {type(e).__name__}: {e}"
        return vazio

    dados = _parse_ficha(texto)
    dados["ok"] = True
    dados["erro"] = ""

    # 7) Fecha o modal pra deixar a pagina limpa pra proxima consulta.
    try:
        fechar = page.get_by_role("button", name="Fechar")
        if fechar.count() > 0:
            fechar.first.click(timeout=3000)
        else:
            page.keyboard.press("Escape")
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
    time.sleep(0.5)

    return dados
