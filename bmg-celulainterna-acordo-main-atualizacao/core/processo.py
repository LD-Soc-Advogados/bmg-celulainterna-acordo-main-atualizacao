"""
Abrir o processo do cliente e extrair o estado do CNJ.

Reaproveitado direto do bmg-extrator (login.py linhas 432-623 e 1742-1783).
"""
import re
import time

from playwright.sync_api import Page

from .frames import _frame_cliente


def abrir_processo_do_cliente(page: Page, timeout_seg: int = 15) -> tuple:
    """Apos clicar no cliente, abre o processo:
       1) clica em 'PROCESSO [N]' (header do bloco de processos)
       2) clica no link do numero do processo (CNJ - so digitos)

    Retorna (True, num_cnj) se conseguiu os dois cliques, ou (False, None).
    O num_cnj e usado para extrair o estado via extrair_estado_do_cnj().
    """
    print("\n=== ABRINDO PROCESSO DO CLIENTE ===")

    # Passo 1: clicar em "PROCESSO [N]" (geralmente [1])
    inicio = time.time()
    clicou_header = False
    while time.time() - inicio < timeout_seg and not clicou_header:
        try:
            fra = _frame_cliente(page)
            for candidato in ["PROCESSO [1]", "PROCESSO ["]:
                loc = fra.get_by_text(candidato, exact=False)
                if loc.count() > 0:
                    loc.first.click()
                    print(f">>> Cabecalho 'PROCESSO [...]' clicado (match: {candidato!r})")
                    clicou_header = True
                    break
        except Exception:
            pass
        if not clicou_header:
            time.sleep(0.5)

    if not clicou_header:
        print(">>> Nao achei 'PROCESSO [N]' no Frame_Processo apos timeout")
        return (False, None)

    # Aguarda expansao do tree
    time.sleep(1.5)

    cnj_encontrado = None
    via_encontrada = None
    inicio = time.time()

    while time.time() - inicio < timeout_seg and not cnj_encontrado:
        for fr in list(page.frames):
            url_fr = fr.url or ""
            nome_fr = fr.name or "?"
            if "nada.htm" in url_fr or "robo.asp" in url_fr:
                continue
            if nome_fr == "FraMenuTop":
                continue
            try:
                resultado = fr.evaluate(r"""() => {
                    // Regex CNJ tolerante: \d{1,7} no 1o segmento (e-Xyon as vezes
                    // mostra sem zeros a esquerda, ex.: "493-75.2016.8.10.0029"
                    // em vez de "0000493-75.2016.8.10.0029").
                    const RE = /\d{1,7}-?\d{2}\.?\d{4}\.?\d\.?\d{2}\.?\d{4}/;
                    // Regex fallback "qualquer numero de processo": digito(s)
                    // + separador (-./) + mais digito(s). Cobre formatos
                    // antigos tipo "2008.001.123456-7", "1234/2008", etc.
                    const RE_GENERICO = /\d+\s*[-./]\s*\d+/;
                    const HEADERS = ['PROCESSO [', 'RECURSO [', 'INCIDENTE [', 'PARTE [', 'PARTES'];
                    function ehHeader(txt) {
                        const up = (txt || '').toUpperCase();
                        return HEADERS.some(h => up.includes(h));
                    }
                    function norm(s) {
                        return (s || '').replace(/[\s ​­]/g, '');
                    }
                    const links = Array.from(document.querySelectorAll('a'));
                    for (const a of links) {
                        const n = norm(a.textContent);
                        const m = n.match(RE);
                        if (m) { a.click(); return {cnj: m[0], via: 'link-texto'}; }
                    }
                    for (const a of links) {
                        const attr = (a.getAttribute('href') || '') +
                                     (a.getAttribute('onclick') || '');
                        const m = attr.match(RE);
                        if (m) { a.click(); return {cnj: m[0], via: 'link-attr'}; }
                    }
                    const containers = Array.from(document.querySelectorAll(
                        '[id="itemContainer"], [onclick*="selectMe"]'
                    ));
                    for (const el of containers) {
                        const n = norm(el.textContent);
                        const m = n.match(RE);
                        if (m) {
                            try { el.click(); } catch(e) {}
                            return {cnj: m[0], via: 'itemContainer'};
                        }
                    }
                    const all = Array.from(document.querySelectorAll('[onclick]'));
                    for (const el of all) {
                        const txt = (el.textContent || '');
                        if (txt.includes('PROCESSO [')) continue;
                        const n = norm(txt);
                        const m = n.match(RE);
                        if (m) {
                            try { el.click(); } catch(e) {}
                            return {cnj: m[0], via: 'onclick-' + el.tagName};
                        }
                    }
                    const bn = norm(document.body.innerText || '');
                    const bm = bn.match(RE);
                    if (bm) return {cnj: bm[0], via: 'body-sem-clique'};

                    // 6) FALLBACK GENERICO: numero de processo fora do padrao
                    //    CNJ. Clica no PRIMEIRO link/onclick com texto curto
                    //    (<=80 chars) que contenha "numero-separador-numero"
                    //    e nao seja header. Garante que SEMPRE clica em algo
                    //    no Frame_Processo, mesmo que o numero foge a qualquer
                    //    padrao reconhecivel.
                    const clicaveis = Array.from(document.querySelectorAll('a, [onclick]'));
                    for (const el of clicaveis) {
                        const txt = (el.textContent || '').trim();
                        if (!txt || txt.length > 80) continue;
                        if (ehHeader(txt)) continue;
                        if (!RE_GENERICO.test(txt)) continue;
                        try {
                            el.click();
                            return {cnj: txt.slice(0, 40), via: 'fallback-generico'};
                        } catch(e) {}
                    }
                    return null;
                }""")
                if resultado and resultado.get("cnj"):
                    cnj_encontrado = resultado["cnj"]
                    via_encontrada = resultado["via"]
                    print(f">>> CNJ encontrado: {cnj_encontrado} em frame '{nome_fr}' [via: {via_encontrada}]")
                    break
            except Exception:
                continue
        if not cnj_encontrado:
            time.sleep(0.5)

    if cnj_encontrado:
        if via_encontrada == "body-sem-clique":
            print(">>> Lido do body — tentando localizar e clicar link...")
            digits = re.sub(r'[^\d]', '', cnj_encontrado)[:7]
            for fr in list(page.frames):
                if "nada.htm" in (fr.url or ""):
                    continue
                try:
                    fr.evaluate(r"""(ini) => {
                        const all = document.querySelectorAll('a, [onclick]');
                        for (const el of all) {
                            const d = (el.textContent || '').replace(/[^\d]/g, '');
                            if (d.includes(ini) && ini.length >= 5) {
                                try { el.click(); return true; } catch(e) {}
                            }
                        }
                        return false;
                    }""", digits)
                except Exception:
                    continue
        return (True, cnj_encontrado)

    # Antes: (False, None) abortava o processamento da CIV.
    # Agora: (True, "") - clicamos PROCESSO [N] com sucesso; CNJ nao bateu
    # padrao tradicional (caso antigo / numero fora do formato CNJ).
    # Caller usa cnj da planilha como fallback. O lote continua.
    print(">>> CNJ nao bateu formato padrao - seguindo sem CNJ (usa o da planilha).")
    return (True, "")


_ESTADOS_TJ = {
    "01": "ACRE", "02": "ALAGOAS", "03": "AMAPA", "04": "AMAZONAS",
    "05": "BAHIA", "06": "CEARA", "07": "DISTRITO FEDERAL",
    "08": "ESPIRITO SANTO", "09": "GOIAS", "10": "MARANHAO",
    "11": "MATO GROSSO", "12": "MATO GROSSO DO SUL", "13": "MINAS GERAIS",
    "14": "PARA", "15": "PARAIBA", "16": "PARANA", "17": "PERNAMBUCO",
    "18": "PIAUI", "19": "RIO DE JANEIRO", "20": "RIO GRANDE DO NORTE",
    "21": "RIO GRANDE DO SUL", "22": "RONDONIA", "23": "RORAIMA",
    "24": "SANTA CATARINA", "25": "SERGIPE", "26": "SAO PAULO",
    "27": "TOCANTINS",
}

_REGIOES_TRF = {
    "01": "FEDERAL TRF1", "02": "FEDERAL TRF2",
    "03": "FEDERAL TRF3", "04": "FEDERAL TRF4",
    "05": "FEDERAL TRF5", "06": "FEDERAL TRF6",
}

_REGIOES_TRT = {f"{i:02d}": f"TRABALHISTA TRT{i}" for i in range(1, 25)}


def extrair_estado_do_cnj(num_processo: str) -> str:
    """Extrai o estado/tribunal do numero CNJ e retorna nome curto.

    Aceita CNJ formatado (com pontos) ou 20 digitos colados.
    Segmento J: 8=TJ, 6=TRF, 5=TRT. TT = código do tribunal.

    Exemplos:
      '5000445-04.2023.8.13.0352' -> 'MINAS GERAIS'
      '50004450420238130352'      -> 'MINAS GERAIS'
      '0001234-55.2021.6.01.0000' -> 'FEDERAL TRF1'
    """
    if not num_processo:
        return "OUTROS"
    s = num_processo.replace(" ", "")

    m = re.search(r'\.(\d)\.(\d{2})\.', s)
    if m:
        segmento = m.group(1)
        tt = m.group(2)
    else:
        digits = re.sub(r'[^\d]', '', s)
        m2 = re.search(r'\d{20}', digits)
        if not m2:
            return "OUTROS"
        d = m2.group(0)
        segmento = d[13]
        tt = d[14:16]

    if segmento == "8":
        return _ESTADOS_TJ.get(tt, f"TJ{tt}_DESCONHECIDO")
    if segmento == "6":
        return _REGIOES_TRF.get(tt, f"FEDERAL_TRF{tt}")
    if segmento == "5":
        return _REGIOES_TRT.get(tt, f"TRABALHISTA_TRT{tt}")
    return f"OUTROS_SEG{segmento}"
