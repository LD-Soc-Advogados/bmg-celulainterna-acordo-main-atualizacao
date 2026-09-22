"""
Tarefa 2 - Baixar documentos via HTTP direto (Zeev), igual bmg-extrator.

Estrategia (descoberta no extrator e validada em producao):
  1. Click no #btnDocumento em Frame_Novo_Processo abre uma popup com a
     tabela `#tbl_ListaDocs`. Cada row tem um elemento com
     `onclick="Baixar('DOC', id_doc, ext, p4)"`.
  2. Extraimos TODOS os (id_doc, p4, tipo_doc) via UM SO evaluate JS na
     popup, depois FECHAMOS a popup. Sem mais cliques de UI.
  3. Pra cada doc alvo, chamamos `baixar_um_via_uuid` (HTTP direto via
     cookies do Edge + curl_cffi). Sem popups encadeadas, sem
     `save_as: canceled` em rede.

Resolve 3 sintomas dos testes em CIV1387277:
  - "Por favor, selecione apenas um item" (era selecao acumulada na UI).
  - "Download.save_as: canceled" (save_as direto em X:\\rede falhava).
  - Re-localizacao fragil de rows entre popups (irrelevante agora).
"""
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from core.config import DOWNLOADS_DIR
from core.frames import _frame_novo_processo
from core.processo import extrair_estado_do_cnj
from core.relatorio import (
    ResultadoLinha,
    STATUS_OK,
    STATUS_ERRO,
    STATUS_JA_FECHADO,
)
from core.zeev_download import (
    baixar_um_via_uuid,
    criar_sessao_zeev,
    bootstrap_uuid_via_disquete,
)


NOME_FLUXO = "Baixar documentos"

# Documentos PRINCIPAIS - o que os advogados realmente precisam.
# Se os 2 estiverem presentes, NADA mais eh baixado (feedback dos
# advogados em 06/2026: 3+ PDFs por CIV eh desnecessario).
DOCUMENTOS_PRINCIPAIS = [
    "PETIÇÃO INICIAL",
    "CÓPIA INTEGRAL",
]

# Documentos COMPLEMENTARES - baixados SOMENTE se faltar algum principal,
# em ordem de preferencia (CITACAO vem antes de INTIMACAO). Baixa apenas
# o primeiro tipo disponivel da lista.
DOCUMENTOS_COMPLEMENTARES = [
    "CITAÇÃO",
    "INTIMAÇÃO",
]

# Compat: lista completa de tipos reconheciveis (usada por _normaliza_tipo)
DOCUMENTOS_ALVO = DOCUMENTOS_PRINCIPAIS + DOCUMENTOS_COMPLEMENTARES

# Slugs ASCII pra nomes de arquivo
SLUG_DOC = {
    "petição inicial":  "peticao_inicial",
    "peticao inicial":  "peticao_inicial",
    "citação":          "citacao",
    "citacao":          "citacao",
    "intimação":        "intimacao",
    "intimacao":        "intimacao",
    "cópia integral":   "copia_integral",
    "copia integral":   "copia_integral",
}

TIMEOUT_POPUP_MS = 15_000


@dataclass
class DocAlvo:
    """Doc alvo extraido da tabela. Tem tudo pra chamar baixar_um_via_uuid."""
    tipo_doc: str          # forma canonica: 'PETIÇÃO INICIAL'
    id_doc: str            # 1o arg do Baixar(...)
    ext: str               # 3o arg (pdf, zip, ...)
    p4: str                # 4o arg (IdEcm); pode vir vazio
    nome_doc: str = ""     # nome do arquivo na tabela (informativo)


def _strip_acentos(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))


def slug_cliente(cliente: str) -> str:
    """NTFS-safe; mantem acento e espaco. NAO usar sozinho pra montar
    pasta de download - use `slug_pasta_processo(cliente, cnj)` pra evitar
    colisao entre homonimos. Continua exposto por compat com scripts que
    so precisam sanitizar o nome."""
    s = (cliente or "DESCONHECIDO").strip()
    for c in '<>:"/\\|?*':
        s = s.replace(c, "")
    s = " ".join(s.split())
    return s or "DESCONHECIDO"


# CNJ tradicional (NNNNNNN-DD.AAAA.J.TR.OOOO) so tem digitos, ponto e hifen -
# ja NTFS-safe. Este regex tira qualquer outro caractere se vier "sujo".
_RE_CNJ_NAO_SAFE = re.compile(r"[^\d\.\-]")


def slug_cnj(cnj: str) -> str:
    """Higieniza o CNJ pra usar em nome de pasta. Mantem so digitos,
    ponto e hifen."""
    return _RE_CNJ_NAO_SAFE.sub("", (cnj or "").strip())


def slug_pasta_processo(cliente: str, cnj: str, civ: str = "") -> str:
    """Nome canonico da pasta de UM processo:

        NOME DA PARTE - 0001234-56.2024.8.13.0000       (com CNJ - preferencia)
        NOME DA PARTE - CIV1234567                       (sem CNJ, com CIV)

    O sufixo GARANTE unicidade por processo. Sem ele, nomes populares
    coincidem entre CIVs diferentes, PDFs se sobrescrevem, e a
    idempotencia por tamanho (>1KB) pula download legitimo por casar
    arquivo do OUTRO processo. A regra 'pulou o download = eh do mesmo
    processo' so eh verdade se a pasta for unica por processo.

    Ordem de preferencia do sufixo:
      1. CNJ  - alem de disambiguar, facilita achar a pasta no drive.
      2. CIV  - raro cair aqui (planilha sem CNJ, ex.: alguns Cifra);
                garante unicidade porque CIV eh unica no e-Xyon.
      3. Sem sufixo - so se cliente E civ vazios (erro de entrada;
                nao acontece em fluxo normal).
    """
    base = slug_cliente(cliente)
    cnj_slug = slug_cnj(cnj)
    if cnj_slug:
        return f"{base} - {cnj_slug}"
    civ_slug = (civ or "").strip().upper()
    if civ_slug:
        return f"{base} - {civ_slug}"
    return base


def slug_doc(tipo: str) -> str:
    """'PETIÇÃO INICIAL' -> 'peticao_inicial'."""
    base = _strip_acentos(tipo or "").strip().lower()
    if base in SLUG_DOC:
        return SLUG_DOC[base]
    return re.sub(r"\s+", "_", base) or "documento"


def _normaliza_tipo(tipo_raw: str) -> str:
    """Mapeia tipo lido da tabela para a forma canonica (com acento).

    Matching PERMISSIVO por palavra-chave: escritorios fazem upload com
    nomes fora do padrao ('INICIAL', 'integral', 'Copia Integral do
    processo', ...). Vale tentar baixar - se vier errado, o operador
    descarta; se nao tentar, fica faltando peca.

    Ordem importa: INICIAL/INTEGRAL (principais) checadas primeiro.
    Retorna '' se nenhum keyword bate.
    """
    if not tipo_raw:
        return ""
    t_ascii = _strip_acentos(tipo_raw.strip().upper())
    if "INICIAL" in t_ascii:
        return "PETIÇÃO INICIAL"
    if "INTEGRAL" in t_ascii:
        return "CÓPIA INTEGRAL"
    if "CITACAO" in t_ascii:
        return "CITAÇÃO"
    if "INTIMACAO" in t_ascii:
        return "INTIMAÇÃO"
    return ""


def _selecionar_para_download(alvos: list, civ: str = "") -> list:
    """Aplica a regra de prioridade dos advogados (06/2026):

    1. PETIÇÃO INICIAL e CÓPIA INTEGRAL sao as pecas que importam.
    2. Se AMBAS presentes -> baixa so elas (complementares ignoradas).
    3. Se faltar alguma -> completa com o primeiro tipo complementar
       disponivel, na ordem CITAÇÃO > INTIMAÇÃO (so UM tipo, nao os dois).
    """
    principais = [d for d in alvos if d.tipo_doc in DOCUMENTOS_PRINCIPAIS]
    tipos_presentes = {d.tipo_doc for d in principais}
    completo = all(t in tipos_presentes for t in DOCUMENTOS_PRINCIPAIS)

    if completo:
        ignorados = [d.tipo_doc for d in alvos if d.tipo_doc not in DOCUMENTOS_PRINCIPAIS]
        if ignorados:
            print(f"  [{civ}] INICIAL+INTEGRAL presentes - ignorando complementares: {ignorados}")
        return principais

    faltando = [t for t in DOCUMENTOS_PRINCIPAIS if t not in tipos_presentes]
    for tipo_comp in DOCUMENTOS_COMPLEMENTARES:
        comps = [d for d in alvos if d.tipo_doc == tipo_comp]
        if comps:
            print(f"  [{civ}] faltando {faltando} - complementando com {tipo_comp} "
                  f"({len(comps)} doc(s))")
            return principais + comps

    if principais:
        print(f"  [{civ}] faltando {faltando} e sem complementar disponivel - "
              f"baixando so o que tem")
    return principais


def _abrir_popup_documentos(page: Page, civ: str):
    """Click em #btnDocumento em Frame_Novo_Processo, captura popup.
    Retorna a Page popup ou None se falhar."""
    try:
        fra = _frame_novo_processo(page)
        btn = fra.locator("#btnDocumento")
        btn.wait_for(state="visible", timeout=TIMEOUT_POPUP_MS)
        with page.expect_popup(timeout=TIMEOUT_POPUP_MS) as popup_info:
            btn.click(timeout=TIMEOUT_POPUP_MS)
        popup = popup_info.value
        popup.wait_for_load_state("domcontentloaded", timeout=TIMEOUT_POPUP_MS)
        time.sleep(0.8)
        return popup
    except PlaywrightTimeout as e:
        print(f"  [{civ}] timeout abrindo popup Documentos: {e}")
        return None
    except Exception as e:
        print(f"  [{civ}] erro abrindo popup Documentos: {type(e).__name__}: {e}")
        return None


def _frame_lista_documentos(popup: Page):
    """Acha o Frame (do Playwright, nao Locator) com `tbl_ListaDocs` ou
    com elementos `onclick='Baixar(...)`. Retorna Frame ou None.

    Portado de bmg-extrator/src/login.py:_frame_lista_documentos.
    """
    for fr in popup.frames:
        try:
            tem = fr.evaluate(r"""() => {
                if (document.getElementById('tbl_ListaDocs')) return true;
                const els = Array.from(document.querySelectorAll('*[onclick]'));
                return els.some(e => (e.getAttribute('onclick') || '').indexOf('Baixar(') !== -1);
            }""")
            if tem:
                return fr
        except Exception:
            continue
    return None


def _listar_documentos_alvo(popup: Page, civ: str) -> list:
    """Extrai todos os docs alvo via UM evaluate JS, igual extrator.

    Para cada elemento com onclick='Baixar(...)' na popup:
      - sobe ate <tr>
      - extrai td[3]=nome_doc, td[4]=tipo_doc (estrutura tbl_ListaDocs)
      - parsea Baixar('DOC', id_doc, ext, p4) com regex
      - filtra pelos DOCUMENTOS_ALVO

    Retorna list[DocAlvo].
    """
    fr = _frame_lista_documentos(popup)
    if fr is None:
        print(f"  [{civ}] nao achei frame com tbl_ListaDocs / onclick='Baixar('")
        return []

    try:
        linhas = fr.evaluate(r"""() => {
            const todos = Array.from(document.querySelectorAll('*[onclick]'));
            const comBaixar = todos.filter(el =>
                (el.getAttribute('onclick') || '').indexOf('Baixar(') !== -1
            );
            const out = [];
            comBaixar.forEach(el => {
                const tr = el.closest('tr');
                if (!tr) return;
                const onclick = el.getAttribute('onclick') || '';
                const m = onclick.match(/Baixar\s*\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)/);
                if (!m) return;
                const tds = Array.from(tr.querySelectorAll('td')).map(t => (t.textContent || '').trim());
                out.push({
                    tipo_doc: tds[4] || '',
                    nome_doc: tds[3] || '',
                    id_doc: m[2],
                    ext: m[3] || 'pdf',
                    p4: m[4] || '',
                });
            });
            return out;
        }""")
    except Exception as e:
        print(f"  [{civ}] erro extraindo lista de docs: {type(e).__name__}: {e}")
        return []

    if not linhas:
        print(f"  [{civ}] nenhuma linha com onclick='Baixar(' na popup")
        return []

    # LOG TEMPORARIO (bug 2 - regra INICIAL estrita): imprime o tipo CRU
    # que o Zeev retorna em td[4]. Rodar 1 planilha, coletar todos os
    # valores distintos, entao substituir _normaliza_tipo por uma regra
    # estrita que rejeita "aditamento a inicial" / "manifestacao sobre
    # inicial" e afins. Remover apos a coleta.
    for linha in linhas:
        print(f"  [{civ}] TIPO_RAW='{linha.get('tipo_doc','')}'  "
              f"nome='{linha.get('nome_doc','')}'")

    alvos = []
    for linha in linhas:
        tipo_canon = _normaliza_tipo(linha.get("tipo_doc", ""))
        if not tipo_canon:
            continue
        alvos.append(DocAlvo(
            tipo_doc=tipo_canon,
            id_doc=linha.get("id_doc", ""),
            ext=(linha.get("ext") or "pdf").lower(),
            p4=linha.get("p4", ""),
            nome_doc=linha.get("nome_doc", ""),
        ))
        print(f"  [{civ}] doc alvo: '{tipo_canon}'  id_doc={linha.get('id_doc')}  "
              f"p4={linha.get('p4') or '(vazio)'}  arquivo={linha.get('nome_doc')}")

    return alvos


def processar_downloads(
    page: Page,
    civ: str,
    cliente: str,
    cnj: str,
    sess_zeev,
    uuid_zeev: str,
    context=None,
) -> tuple:
    """Lista docs alvo (1 popup) + baixa cada um via HTTP (sem mais UI).

    sess_zeev/uuid_zeev: criados UMA vez no main; se vier (None, None) e
    `context` estiver disponivel, faz WARMUP automatico: abre a popup
    #btnDocumento (que de qualquer forma teria que abrir), espera 2s
    pro servidor Zeev setar o cookie ConvergeSilentLogin_* no contexto,
    e recaptura via criar_sessao_zeev(context). Sem necessidade de
    clique manual previo no Edge dedicado.

    Retorna (ResultadoLinha, sess_zeev_atualizado, uuid_zeev_atualizado).
    O main usa os 2 ultimos pra manter a sessao quente nas proximas CIVs.
    """
    estado = extrair_estado_do_cnj(cnj) if cnj else ""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _resultado(status: str, msg: str) -> ResultadoLinha:
        return ResultadoLinha(
            civ=civ, cliente=cliente, cnj=cnj, estado=estado,
            fluxo=NOME_FLUXO, status=status, mensagem=msg,
            timestamp=timestamp,
        )

    # 1) Abre popup
    popup = _abrir_popup_documentos(page, civ)
    if popup is None:
        return (
            _resultado(STATUS_ERRO, "Nao consegui abrir popup de Documentos"),
            sess_zeev, uuid_zeev,
        )

    # 1a) Warmup Zeev: se nao temos UUID, a abertura da popup talvez
    # tenha setado o cookie agora. Tenta primeiro o caminho passivo
    # (so esperar). Se nao bater, faz bootstrap ativo clicando o 1o
    # disquete da popup via JS - igual o extrator faz.
    if (sess_zeev is None or not uuid_zeev) and context is not None:
        print(f"  [{civ}] UUID Zeev ausente - tentando warmup...")
        time.sleep(2.0)
        new_sess, new_uuid = None, None
        try:
            new_sess, new_uuid = criar_sessao_zeev(context)
        except Exception:
            pass

        if new_uuid:
            print(f"  [{civ}] OK warmup passivo: UUID {new_uuid[:8]}...{new_uuid[-4:]}")
            sess_zeev = new_sess
            uuid_zeev = new_uuid
        else:
            # Passivo nao serviu. Bootstrap ativo: clica disquete via JS.
            try:
                novo_uuid = bootstrap_uuid_via_disquete(popup, context, civ)
            except Exception as e:
                print(f"  [{civ}] erro no bootstrap: {type(e).__name__}: {e}")
                novo_uuid = None
            if novo_uuid:
                try:
                    new_sess, _ = criar_sessao_zeev(context)
                except Exception:
                    new_sess = None
                if new_sess is not None:
                    sess_zeev = new_sess
                    uuid_zeev = novo_uuid
                    print(f"  [{civ}] OK bootstrap: UUID {novo_uuid[:8]}...{novo_uuid[-4:]}")

    if sess_zeev is None or not uuid_zeev:
        try:
            popup.close()
        except Exception:
            pass
        return (
            _resultado(
                STATUS_ERRO,
                "Sessao Zeev nao disponivel (sem cookie ConvergeSilentLogin "
                "apos warmup; clique 1 disquete manualmente no Edge e rode de novo)",
            ),
            sess_zeev, uuid_zeev,
        )

    # 2) Extrai os alvos da popup, depois fecha
    try:
        alvos = _listar_documentos_alvo(popup, civ)
    finally:
        try:
            popup.close()
        except Exception:
            pass

    if not alvos:
        return (
            _resultado(
                STATUS_JA_FECHADO,
                "Nenhum documento alvo (INICIAL/INTEGRAL/CITACAO/INTIMACAO) presente",
            ),
            sess_zeev, uuid_zeev,
        )

    # Regra de prioridade dos advogados: INICIAL+INTEGRAL bastam;
    # complementar (CITACAO > INTIMACAO) so quando falta principal.
    alvos = _selecionar_para_download(alvos, civ)

    pasta_cliente = Path(DOWNLOADS_DIR) / slug_pasta_processo(cliente, cnj, civ)
    try:
        pasta_cliente.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return (
            _resultado(
                STATUS_ERRO,
                f"Falha criando pasta {pasta_cliente}: {type(e).__name__}: {e}",
            ),
            sess_zeev, uuid_zeev,
        )

    # 2) Para cada alvo, chama HTTP direto (sem mais popup)
    contagem_slug = {}
    baixados, pulados, falhas = [], [], []

    for doc in alvos:
        base_slug = slug_doc(doc.tipo_doc)
        n = contagem_slug.get(base_slug, 0) + 1
        contagem_slug[base_slug] = n
        slug = base_slug if n == 1 else f"{base_slug}_{n}"
        # nome alvo - baixar_um_via_uuid pode mudar a extensao baseado no conteudo
        nome_arquivo = f"{slug}.{doc.ext or 'pdf'}"
        path_alvo = pasta_cliente / f"{slug}.pdf"

        # Idempotencia: pula se ja existe um arquivo VALIDO (>1KB) com o
        # mesmo nome. 1KB e mais seguro que >0 pq pega corrupcao parcial
        # (download que travou no meio da rede e salvou so headers). PDFs
        # reais sao sempre dezenas/centenas de KB no minimo.
        if path_alvo.exists():
            tam_local = path_alvo.stat().st_size
            if tam_local > 1024:
                pulados.append(slug)
                print(f"  [{civ}] {path_alvo.name} ja existe ({tam_local/1024:.1f} KB) - pulando")
                continue
            else:
                print(f"  [{civ}] {path_alvo.name} existe mas eh pequeno demais "
                      f"({tam_local} bytes) - baixando de novo")

        try:
            resultado_path = baixar_um_via_uuid(
                sess_zeev, uuid_zeev,
                doc.id_doc, doc.p4, doc.ext or "pdf",
                pasta_cliente, nome_arquivo,
            )
            if resultado_path is None:
                falhas.append(slug)
                print(f"  [{civ}] FALHA {slug} (id_doc={doc.id_doc})")
            else:
                baixados.append(resultado_path.name)
                tam = resultado_path.stat().st_size
                print(f"  [{civ}] OK {resultado_path.name} ({tam/1024:.1f} KB)")
        except Exception as e:
            falhas.append(slug)
            print(f"  [{civ}] EXCECAO {slug}: {type(e).__name__}: {e}")

        time.sleep(0.5)  # pausa cordial entre downloads HTTP

    partes = []
    if baixados:
        partes.append(f"baixados={baixados}")
    if pulados:
        partes.append(f"pulados={pulados}")
    if falhas:
        partes.append(f"falhas={falhas}")
    msg_resumo = "; ".join(partes) or "nenhum doc processado"

    if falhas and not baixados:
        return _resultado(STATUS_ERRO, msg_resumo), sess_zeev, uuid_zeev
    if baixados:
        return (
            _resultado(STATUS_ERRO if falhas else STATUS_OK, msg_resumo),
            sess_zeev, uuid_zeev,
        )
    return _resultado(STATUS_JA_FECHADO, msg_resumo), sess_zeev, uuid_zeev
