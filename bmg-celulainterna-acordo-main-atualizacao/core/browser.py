"""
Conexão CDP com o Edge dedicado.

O e-Xyon do BMG tem anti-bot que bloqueia browsers headless ou lançados
pelo Playwright. Estratégia única: o operador abre o Edge via
abrir-edge-bmg.bat (porta 9222), loga manualmente, e o script ataca via CDP.

Nunca lançamos um browser direto aqui. Sem fallback. Quem chamar sem ter
o Edge aberto recebe RuntimeError claro.
"""
import time

try:
    from playwright_stealth import Stealth
except Exception:  # pragma: no cover - fallback seguro se a lib nao existir
    Stealth = None

from .config import CDP_URL, TIMEOUT_MS


def aplicar_stealth_ao_contexto(contexto) -> bool:
    """Aplica os evasions do playwright-stealth ao BrowserContext."""
    if contexto is None or Stealth is None:
        return False
    try:
        stealth = Stealth()
        stealth.apply_stealth_sync(contexto)
        return True
    except Exception as exc:  # pragma: no cover - depende do navegador em runtime
        print(f"  [stealth] falha ao aplicar ao contexto: {type(exc).__name__}: {exc}")
        return False


def aplicar_stealth_ao_page(page) -> bool:
    """Aplica os evasions do playwright-stealth a uma Page se disponível."""
    if page is None or Stealth is None:
        return False
    try:
        stealth = Stealth()
        stealth.apply_stealth_sync(page)
        return True
    except Exception as exc:  # pragma: no cover - depende do navegador em runtime
        print(f"  [stealth] falha ao aplicar na página: {type(exc).__name__}: {exc}")
        return False


def _eh_aba_excluida(url: str) -> bool:
    """True se a aba NAO deve ser usada como base (popup, viewer, etc).
    Copiado da lógica do bmg-extrator (main, CDP mode)."""
    if not url:
        return True
    if "popup/" in url:
        return True
    if "econtent.bancobmg.com.br" in url:
        return True
    if "/popup/pop.asp" in url:
        return True
    if "documento_causa_list_pop" in url:
        return True
    return False


def conectar_edge_cdp(playwright):
    """Conecta ao Edge já aberto via CDP e retorna (browser, context, page).

    Pre-requisito: o operador rodou `abrir-edge-bmg.bat`, logou no e-Xyon,
    passou pela tela de unidade, e está em `_FSet_Main.asp` (ou qualquer aba
    do domínio bancobmg.com.br fora popup/viewer).

    A página retornada já tem `set_default_timeout(TIMEOUT_MS)` aplicado e
    `bring_to_front()` chamado.
    """
    print(f">>> Conectando ao Edge via CDP em {CDP_URL} ...")
    try:
        browser = playwright.chromium.connect_over_cdp(CDP_URL)
    except Exception as e:
        raise RuntimeError(
            f"Falha ao conectar via CDP em {CDP_URL}: {e}\n"
            f"Voce abriu o Edge com 'abrir-edge-bmg.bat'? "
            f"Confirme que a janela esta aberta e a porta 9222 livre."
        ) from e

    contexts = browser.contexts
    if not contexts:
        raise RuntimeError(
            "Nenhum contexto aberto no Edge. "
            "Abra o Edge com abrir-edge-bmg.bat e faca login antes de rodar."
        )
    context = contexts[0]

    print(f">>> Abas detectadas no Edge: {len(context.pages)}")
    page = None
    for tentativa in range(10):  # até ~5s
        pages = context.pages
        if tentativa == 0:
            for i, pg in enumerate(pages):
                url = pg.url or "(vazia)"
                marcar = "  [excluida]" if _eh_aba_excluida(url) else ""
                print(f"     aba[{i}]: {url[:120]}{marcar}")

        candidatos = [pg for pg in pages if not _eh_aba_excluida(pg.url or "")]

        # Prioridade 1: aba na tela principal pós-login
        for pg in candidatos:
            if "_FSet_Main.asp" in (pg.url or ""):
                page = pg
                break
        if page is None:
            # Prioridade 2: qualquer aba bancobmg sem ser popup
            for pg in candidatos:
                if "bancobmg.com.br" in (pg.url or ""):
                    page = pg
                    break
        if page is not None:
            break
        time.sleep(0.5)

    if page is None:
        raise RuntimeError(
            "Nenhuma aba do e-Xyon esta aberta no Edge. "
            "Confirme que voce logou e passou pela tela de unidade."
        )

    print(f">>> Aba escolhida: {page.url[:120]}")
    aplicar_stealth_ao_contexto(context)
    aplicar_stealth_ao_page(page)
    try:
        page.bring_to_front()
    except Exception:
        pass
    page.set_default_timeout(TIMEOUT_MS)
    return browser, context, page
