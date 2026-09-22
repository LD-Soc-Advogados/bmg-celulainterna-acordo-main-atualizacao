"""
Navegação do menu lateral do e-Xyon e telas de pré-busca:
  - Detecção de tela de login / unidade
  - Clique nos itens JURIDICO -> PESQUISA CAUSA
  - Espera por login manual (fallback se o operador esqueceu de logar)
"""
import time

from playwright.sync_api import Page


def existe_campo_senha(page: Page) -> bool:
    """Heuristica: se ha input[type=password] visivel em qualquer frame,
    provavelmente ainda estamos na tela de login."""
    for frame in page.frames:
        try:
            n = frame.evaluate("() => document.querySelectorAll('input[type=password]').length")
            if n and int(n) > 0:
                return True
        except Exception:
            continue
    return False


def estamos_em_unidade(page: Page) -> bool:
    """Verifica se estamos na tela de selecao de unidade (Unidade.asp)."""
    return "unidade.asp" in (page.url or "").lower()


def sessao_expirada(page: Page) -> bool:
    """True se a sessao do e-Xyon expirou e a pagina voltou pra login.

    O e-Xyon desloga apos ~20min de inatividade. Quando isso acontece, a
    URL volta pra /Logon/Default.asp, /Logon/Unidade.asp, ou aparece campo
    senha em algum frame.

    Usado entre CIVs no loop principal: se detectado, aborta o lote limpo
    em vez de gerar dezenas de "TargetClosedError" em cascata.
    """
    try:
        url = (page.url or "").lower()
    except Exception:
        # Se ate page.url ja explode, o context fechou - tambem trata como expirado
        return True
    if "/logon/" in url or "/login" in url or url.endswith("/default.asp"):
        return True
    # Caso a URL ainda mostre Principal mas algum frame tenha redirecionado
    # pra login, existe_campo_senha pega isso.
    try:
        if existe_campo_senha(page):
            return True
    except Exception:
        pass
    return False


def clicar_entrar_unidade(page: Page) -> bool:
    """Tela de selecao de unidade: CIVEL ja vem pre-selecionada, so clica em Entrar."""
    seletores = [
        "#btEntrar",
        "input[type='button'][value='Entrar' i]",
        "input[type='submit'][value='Entrar' i]",
        "#btOK",
        "input[type='button'][value='OK' i]",
        "button:has-text('Entrar')",
        "a:has-text('Entrar')",
    ]
    for frame in page.frames:
        for sel in seletores:
            try:
                loc = frame.locator(sel)
                if loc.count() > 0:
                    print(f"Botao Entrar encontrado via {sel} (frame '{frame.name}')")
                    loc.first.click()
                    return True
            except Exception:
                continue
    print("ATENCAO: nao achei botao Entrar na tela de unidade.")
    return False


def aguardar_login_manual(page: Page, timeout_total_seg: int = 300) -> bool:
    """Fallback caso o operador rode o script sem ter feito login.

    Detecta sucesso quando o campo senha some da pagina. Em uso normal
    (operador seguiu o procedimento abrir-edge-bmg.bat → login → unidade),
    `existe_campo_senha` retorna False imediatamente e esta funcao nem
    chega a ser chamada.
    """
    print()
    print("=" * 64)
    print("  LOGIN NAO DETECTADO - FACA NA JANELA DO EDGE")
    print("=" * 64)
    print("  1. Va para a janela do Edge dedicado")
    print("  2. Digite usuario, senha (e codigo de seguranca se aparecer)")
    print("  3. Clique em OK e selecione a unidade")
    print("=" * 64)
    print(f"  (timeout: ate {timeout_total_seg}s)")
    print()

    inicio = time.time()
    ultimo_aviso = 0
    while time.time() - inicio < timeout_total_seg:
        try:
            if not existe_campo_senha(page):
                decorrido = int(time.time() - inicio)
                print(f"\n>>> Login detectado em {decorrido}s. URL atual: {page.url}")
                return True
        except Exception:
            pass
        time.sleep(1)
        decorridos = int(time.time() - inicio)
        if decorridos > 0 and decorridos % 30 == 0 and decorridos != ultimo_aviso:
            ultimo_aviso = decorridos
            print(f"  ...aguardando login ({decorridos}s)")

    print(f"\nTimeout: voce nao logou em {timeout_total_seg}s.")
    return False


def clicar_item_menu(page: Page, rotulo: str, variantes: list = None) -> bool:
    """Tenta clicar num item de menu pelo texto/alt/onclick.
    Procura em todos os frames. Usado para JURIDICO e PESQUISA CAUSA."""
    if variantes is None:
        variantes = [rotulo]

    seletores = []
    for v in variantes:
        v_safe = v.replace("'", "\\'")
        seletores.extend([
            f"a:has-text('{v_safe}')",
            f"a:text-is('{v_safe}')",
            f"td:has-text('{v_safe}')",
            f"span:has-text('{v_safe}')",
            f"img[alt='{v_safe}']",
            f"img[alt*='{v_safe}' i]",
            f"[onclick*='{v_safe}']",
        ])

    for frame in page.frames:
        for sel in seletores:
            try:
                loc = frame.locator(sel)
                if loc.count() > 0:
                    print(f"Item '{rotulo}' encontrado via {sel} (frame '{frame.name}')")
                    loc.first.click()
                    return True
            except Exception:
                continue
    print(f"Nao achei item de menu: {rotulo}")
    return False


def clicar_juridico(page: Page) -> bool:
    """Clica no item de menu JURIDICO (com ou sem acento)."""
    return clicar_item_menu(page, "JURIDICO", variantes=[
        "JURÍDICO", "JURIDICO", "Jurídico", "Juridico"
    ])


def clicar_pesquisa_causa(page: Page) -> bool:
    """Clica no submenu PESQUISA CAUSA (sem 'DE')."""
    return clicar_item_menu(page, "PESQUISA CAUSA", variantes=[
        "PESQUISA CAUSA", "Pesquisa Causa", "Pesquisa causa", "pesquisa causa"
    ])
