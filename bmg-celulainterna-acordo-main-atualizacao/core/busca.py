"""
Busca de CIV no e-Xyon: digita o código no form de pesquisa, captura o
nome do cliente do primeiro resultado e clica nele.

Reaproveitado direto do bmg-extrator (login.py linhas 316-429), confirmado
em produção.
"""
import re
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page

from .frames import _frame_pesquisa, _frame_resultado
from .config import LOGS_DIR


_PALAVRAS_PROIBIDAS = {
    "SAIR", "VOLTAR", "CANCELAR", "OK", "PESQUISAR", "EXCLUIR", "EDITAR",
    "PESQUISA", "CAUSA", "CONSULTA", "BANCO", "BMG", "CLIENTE",
    "NOME", "CPF", "CIV", "PROCESSO", "STATUS", "EXIBIR", "CLIQUE",
    "GERENCIA", "GERENCIADOR", "RESULTADO", "PAGINA", "SELECIONE",
    "JURIDICO", "JURÍDICO", "FINANCEIRO", "RELATORIOS", "RELATÓRIOS",
}


def normalizar_civ(civ_input: str) -> tuple:
    """Aceita CIV em qualquer formato comum (com ou sem prefixo CIV).

    Entrada: 'CIV1104354', 'civ1104354', '1104354', ' CIV 1104354 '.
    Retorna: (civ_completo, numero_apenas)
             ex.: ('CIV1104354', '1104354')
    """
    s = (civ_input or "").strip().upper().replace(" ", "")
    if s.startswith("CIV"):
        numero = s[3:]
    else:
        numero = s
    return (f"CIV{numero}", numero)


def buscar_civ_exato(page: Page, civ_completo: str) -> bool:
    """Faz a busca usando os seletores exatos descobertos no codegen.

    civ_completo: string como 'CIV1104354' (com prefixo — o e-Xyon aceita).
    """
    print(f"\n--- Busca via seletores exatos ---")
    try:
        fra = _frame_pesquisa(page)
        fra.locator("#cmbBusca").wait_for(state="visible", timeout=15000)

        fra.locator("#cmbBusca").select_option("C")
        print("Dropdown #cmbBusca: 'C' (Codigo Causa) selecionado")

        campo = fra.locator("#txtPesquisa")
        campo.click()
        campo.fill("")
        campo.fill(civ_completo)
        print(f"Campo #txtPesquisa: '{civ_completo}' preenchido")

        fra.get_by_role("button", name="Pesquisar").click()
        print("Botao Pesquisar clicado")
        return True
    except Exception as e:
        print(f"Erro na busca exata: {e}")
        return False


def _texto_parece_nome(t: str) -> bool:
    """True se a string parece um nome de pessoa (TUDO MAIUSCULO + ESPACO)."""
    if not t or len(t) < 5 or len(t) > 80:
        return False
    if " " not in t:
        return False
    if not re.match(r"^[A-ZÀ-Ý]", t):
        return False
    if re.search(r"\d", t):
        return False
    letras = [c for c in t if c.isalpha()]
    if not letras:
        return False
    maiusculas = [c for c in letras if c.isupper()]
    if len(maiusculas) / len(letras) < 0.8:
        return False
    palavras = t.upper().split()
    if any(p in _PALAVRAS_PROIBIDAS for p in palavras):
        return False
    return True


def clicar_cliente_e_capturar_nome(page: Page, timeout_seg: int = 15) -> str:
    """Espera o resultado da busca aparecer (tr[id^='linha_']), extrai o nome
    do cliente da primeira linha, clica nela. Retorna o nome capturado."""
    print("Aguardando resultado da busca aparecer (tr#linha_1)...")
    inicio = time.time()

    while time.time() - inicio < timeout_seg:
        try:
            fra = _frame_resultado(page)
            primeira_linha = fra.locator("tr[id^='linha_']").first
            if primeira_linha.count() > 0:
                texto_linha = primeira_linha.evaluate(
                    "(el) => (el.textContent || '').trim()"
                )
                campos = [c.strip() for c in re.split(r"\s{2,}", texto_linha) if c.strip()]
                nome = next((c for c in campos if _texto_parece_nome(c)), "")
                if not nome and campos:
                    nome = campos[0]
                print(f">>> Linha encontrada: {campos}")
                print(f">>> Nome do cliente identificado: '{nome}'")
                primeira_linha.click()
                print(">>> Cliente clicado (tr#linha_1)")
                return nome
        except Exception:
            pass
        time.sleep(0.5)

    print(f"Nao identifiquei tr#linha_N apos {timeout_seg}s")
    try:
        # FrameLocator nao tem .evaluate(); usa .locator(body) que tem.
        fra = _frame_resultado(page)
        dump = fra.locator("body").evaluate("""(body) => {
            return (body.innerText || '(sem body)').substring(0, 4000);
        }""")
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = LOGS_DIR / f"05b-fraResultado-{ts}.txt"
        path.write_text(dump, encoding="utf-8")
        print(f">>> Conteudo do fraResultado salvo em: {path}")
    except Exception as e:
        print(f">>> Nao consegui dumpar fraResultado: {e}")
    return ""
