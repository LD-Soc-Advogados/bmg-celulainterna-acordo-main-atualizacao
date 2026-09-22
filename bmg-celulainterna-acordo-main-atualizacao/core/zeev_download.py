"""
Download HTTP de documentos via Zeev (econtent.bancobmg.com.br).

Extraido de bmg-extrator/src/baixar_http.py (versoes v43-v50). Mantem as
4 funcoes que o operador real usa em fluxo de lote:

  - pegar_cookies_do_edge()
  - tentar_gerar_uuid_via_http(sess)
  - criar_sessao_zeev(context=None)
  - baixar_um_via_uuid(sess, uuid_zeev, id_doc, p4, ext, pasta, nome)

NAO inclui as funcoes de CLI standalone (baixar() / baixar_reusando_sessao_zeev())
do extrator - aqui usamos sempre o fluxo embarcado (com context Playwright
ja aberto pelo robo).

Pre-requisito (descoberta empirica dos irmaos):
  - curl_cffi imita o TLS/HTTP fingerprint do Chrome 146; requests "puro"
    e detectado pelo Zeev via JA3 e bloqueado.
  - Os cookies do Edge dedicado precisam estar quentes (login feito).
"""
import io
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path

from curl_cffi import requests as cffi_requests
from playwright.sync_api import sync_playwright

from .config import CDP_URL


HEADERS_DEFAULT = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "sec-ch-ua": '"Chromium";v="148", "Microsoft Edge";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


def pegar_cookies_do_edge() -> list:
    """Conecta no Edge via CDP e retorna lista de cookies do bancobmg.com.br.

    Usado quando NAO ha um context Playwright ja aberto (modo standalone).
    No fluxo embarcado prefira criar_sessao_zeev(context=context).
    """
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        if not browser.contexts:
            raise RuntimeError(
                "Nenhum contexto Edge. Abra o Edge com abrir-edge-bmg.bat."
            )
        cookies = browser.contexts[0].cookies()
        browser.close()
    return [c for c in cookies if "bancobmg.com.br" in c.get("domain", "")]


def tentar_gerar_uuid_via_http(sess, id_doc_qualquer: str = "20243497"):
    """Gera um UUID Zeev via HTTP (POST SilentRedirect.aspx -> SilentAccess.aspx).

    Funciona se a sessao Edge esta logada no e-Xyon. Usa um id_doc "qualquer"
    como pretexto (o default eh um SUBSIDIOS testado no extrator). O servidor
    Zeev cria a sessao baseado nos cookies do Edge + XML enviado.

    Retorna o UUID novo se conseguiu, None se falhou.
    """
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    xml = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>'
        '<FILTER>\t'
        '<USER>CVGWEB</USER>\t'
        '<WEBSERVER>http://convergeecm:82/SilentRedirect.aspx</WEBSERVER>\t'
        '<APPLICATION>APPDOCSJREX</APPLICATION>\t'
        '<FIELDS>\t\t'
        '<ID_DOC>\t\t\t'
        f'<VALUE>{id_doc_qualquer}</VALUE>\t\t'
        '</ID_DOC>\t'
        '</FIELDS>\t'
        f'<PERMISSION MouseRightButton="0" ImagePrint="0" ImageDrag="0" '
        f'EnableSearch="0" CopyMemory="0" TimeStamp="{ts}" />'
        '</FILTER>'
    )

    try:
        r1 = sess.post(
            "https://econtent.bancobmg.com.br/SilentRedirect.aspx",
            data={"XML": xml},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://juridico.bancobmg.com.br",
                "Referer": "https://juridico.bancobmg.com.br/popup/documento_causa_list_pop.asp",
            },
            allow_redirects=False, timeout=15,
        )
        if r1.status_code != 200:
            return None
        m = re.search(
            r"<form[^>]*action=['\"]SilentAccess\.aspx['\"][^>]*>.*?"
            r"<input[^>]+name=['\"]xml['\"][^>]+value=['\"]([^'\"]+)['\"]",
            r1.text, re.IGNORECASE | re.DOTALL,
        )
        if not m:
            return None
        xml_codificado = m.group(1)
    except Exception:
        return None

    try:
        r2 = sess.post(
            "https://econtent.bancobmg.com.br/SilentAccess.aspx",
            data={"xml": xml_codificado},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://econtent.bancobmg.com.br",
                "Referer": "https://econtent.bancobmg.com.br/SilentRedirect.aspx",
                "Upgrade-Insecure-Requests": "1",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            allow_redirects=True, timeout=30,
        )
        m = re.search(
            r"silent/search/home/(?:appdocsjrex/)?([a-f0-9-]{36})", r2.url
        )
        if m and "/error/" not in r2.url.lower():
            return m.group(1)
    except Exception:
        return None

    return None


def bootstrap_uuid_via_disquete(popup, context, civ: str = ""):
    """Clica o primeiro <img onclick='Baixar(...)'> da popup via JS pra
    forcar o servidor Zeev a setar o cookie ConvergeSilentLogin_*.

    Portado de bmg-extrator/src/login.py:_auto_bootstrap_uuid_via_disquete.

    Estrategia: usar `document.querySelector('img[onclick*=\"Baixar(\"]').click()`
    via JS evaluate dispara o handler onclick que faz o request pro
    econtent.bancobmg.com.br - dai o cookie eh setado no contexto.
    Polling de ate 10s pelo UUID via criar_sessao_zeev(context).

    Retorna o UUID gerado, ou None se nao deu.
    """
    import time as _time
    print(f"  [{civ}] bootstrap: clicando 1o disquete via JS pra criar cookie Zeev...")

    # Encontra o frame que tem onclick='Baixar(' (igual extrator)
    fr_docs = None
    for fr in popup.frames:
        try:
            tem = fr.evaluate(r"""() => {
                if (document.getElementById('tbl_ListaDocs')) return true;
                const els = Array.from(document.querySelectorAll('*[onclick]'));
                return els.some(e => (e.getAttribute('onclick') || '').indexOf('Baixar(') !== -1);
            }""")
            if tem:
                fr_docs = fr
                break
        except Exception:
            continue

    if fr_docs is None:
        print(f"  [{civ}] frame da lista de docs nao encontrado pro bootstrap")
        return None

    # Snapshot das pages antes pra fechar as novas (o click no disquete
    # provavelmente abre janela viewer Zeev)
    pages_antes = set(id(p) for p in context.pages) if context is not None else set()

    # Click no primeiro disquete via JS
    try:
        clicou = fr_docs.evaluate(r"""() => {
            const img = document.querySelector('img[onclick*="Baixar("]');
            if (!img) return false;
            try { img.scrollIntoView({block: 'center'}); } catch(e) {}
            img.click();
            return true;
        }""")
    except Exception as e:
        print(f"  [{civ}] erro clicando disquete via JS: {type(e).__name__}: {e}")
        return None

    if not clicou:
        print(f"  [{civ}] nenhum disquete encontrado pra clicar (popup vazia?)")
        return None

    # Polling: aguarda cookie aparecer (ate 20s - aumentado de 10s
    # porque algumas vezes o servidor demora a criar o cookie)
    uuid_zeev = None
    for tentativa in range(40):
        _time.sleep(0.5)
        try:
            _, uuid_zeev = criar_sessao_zeev(context=context)
            if uuid_zeev:
                print(f"  [{civ}] cookie Zeev capturado em {(tentativa+1)*0.5:.1f}s")
                break
        except Exception:
            continue

    # Fecha qualquer page nova que apareceu (janela viewer do Zeev)
    if context is not None:
        try:
            for p in list(context.pages):
                if id(p) not in pages_antes:
                    try:
                        p.close()
                    except Exception:
                        pass
        except Exception:
            pass

    if uuid_zeev:
        return uuid_zeev
    print(f"  [{civ}] timeout (20s): cookie Zeev nao apareceu apos click no disquete")
    return None


def criar_sessao_zeev(context=None):
    """Cria sessao curl_cffi com cookies do Edge + identifica UUID Zeev mais novo.

    Retorna (sess, uuid_zeev) ou (None, None) se falhou.

    Se `context` for fornecido (Playwright context ja aberto), pega os
    cookies direto dele. Caso contrario, abre uma nova conexao CDP.

    O UUID Zeev mais novo eh extraido do cookie `ConvergeSilentLogin_{UUID}`
    com maior 'expires'. Se nenhum cookie desse tipo existe, retorna
    (sess_sem_uuid, None) - o chamador pode tentar tentar_gerar_uuid_via_http().
    """
    if context is not None:
        cookies = context.cookies()
        cookies_bmg = [c for c in cookies if "bancobmg.com.br" in c.get("domain", "")]
    else:
        cookies_bmg = pegar_cookies_do_edge()

    sess = cffi_requests.Session(impersonate="chrome146")
    sess.headers.update(HEADERS_DEFAULT)
    for c in cookies_bmg:
        sess.cookies.set(
            c["name"], c["value"],
            domain=c.get("domain"), path=c.get("path", "/"),
        )

    uuids = []
    for c in cookies_bmg:
        m = re.search(r"ConvergeSilentLogin_([a-f0-9-]{36})", c.get("name", ""))
        if m:
            uuids.append({"uuid": m.group(1), "expires": c.get("expires", 0)})

    if not uuids:
        return sess, None

    uuids.sort(key=lambda x: x["expires"], reverse=True)
    return sess, uuids[0]["uuid"]


def baixar_um_via_uuid(
    sess,
    uuid_zeev: str,
    id_doc: str,
    p4: str,
    ext: str,
    pasta_destino: Path,
    nome_destino: str,
) -> Path:
    """Baixa UM documento via Zeev (sessao curl_cffi + UUID ja existentes).

    Se p4 vier vazio, busca via SearchData (campo `f` do JSON).

    Aceita qualquer conteudo binario nao-HTML (PDF, ZIP, MP3, MP4, ...).
    HTML eh tratado como erro (provavel tela de login/erro do Zeev).

    Retorna Path do arquivo salvo, ou None se falhou.
    """
    pasta_destino = Path(pasta_destino)
    pasta_destino.mkdir(parents=True, exist_ok=True)

    home_url = (
        f"https://econtent.bancobmg.com.br/silent/search/home/"
        f"appdocsjrex/{uuid_zeev}"
    )

    # 1) SearchData - valida o doc + (se preciso) extrai p4
    search_body = {
        "echo": 1, "start": 0, "length": 10, "direction": "asc", "sort": "",
        "filters": json.dumps([
            {"Name": "ID_DOC", "Value": str(id_doc), "MultiSelect": False,
             "Display": str(id_doc)}
        ]),
        "applicationId": "45", "applicationCode": "APPDOCSJREX",
        "edit": False, "typeSearch": "basic", "showDeleted": True,
        "protocol": uuid_zeev, "embedded": False,
        "codFlowExecuteUid": None, "codFlowExecute": None,
        "codFlowExecuteTaskUid": None, "codFlowExecuteTask": None,
    }
    rs = sess.post(
        "https://econtent.bancobmg.com.br/Services/Search/Search.asmx/SearchData",
        json=search_body,
        headers={
            "Content-Type": "application/json; charset=UTF-8",
            "Origin": "https://econtent.bancobmg.com.br",
            "Referer": home_url,
            "X-Requested-With": "XMLHttpRequest",
            "X-Alt-Referer": home_url,
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
        timeout=20,
    )
    if rs.status_code != 200:
        print(f"  ! SearchData falhou (status={rs.status_code})")
        return None

    if not p4 or p4.strip() == "":
        try:
            data = rs.json()
            if isinstance(data, dict) and "d" in data:
                data = data["d"]
            if isinstance(data, str):
                data = json.loads(data)
            aa_data = data.get("aaData", [])
            if isinstance(aa_data, str):
                aa_data = json.loads(aa_data)
            if aa_data and isinstance(aa_data, list) and len(aa_data) > 0:
                p4 = str(aa_data[0].get("f", ""))
                if p4:
                    print(f"  > p4 extraido dinamicamente: {p4}")
        except Exception as e:
            print(f"  ! Nao consegui extrair p4 do SearchData: {e}")
    if not p4:
        print(f"  ! Sem p4 (IdEcm). Nao da pra construir download URL.")
        return None

    viewer_url = (
        f"https://econtent.bancobmg.com.br/silent/viewer/"
        f"appdocsjrex/45/{p4}/0/0/{uuid_zeev}"
    )
    download_url = (
        f"https://econtent.bancobmg.com.br/attach.downfile.zip"
        f"?&fid=&fappid=45&fappcode=APPDOCSJREX&fids={p4},0"
    )

    rd = sess.get(
        download_url,
        headers={
            "Accept": "*/*",
            "Referer": viewer_url,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "same-origin",
        },
        timeout=60,
        allow_redirects=True,
    )
    if rd.status_code != 200 or len(rd.content) < 100:
        print(f"  ! Download falhou (status={rd.status_code}, size={len(rd.content)})")
        return None

    primeiros4 = rd.content[:4]
    ct = rd.headers.get("content-type", "").lower()
    if "text/html" in ct or primeiros4 == b"<htm" or primeiros4 == b"<!DO":
        print(f"  ! Conteudo eh HTML (provavel erro/login): ct={ct}")
        return None

    # Se for ZIP, descompacta o 1o PDF de dentro (Zeev entrega ZIP contendo o PDF)
    payload = rd.content
    nome_arquivo = nome_destino
    if primeiros4 == b"PK\x03\x04" or "zip" in ct:
        try:
            with zipfile.ZipFile(io.BytesIO(rd.content)) as zf:
                pdfs = [n for n in zf.namelist() if n.lower().endswith(".pdf")]
                if pdfs:
                    with zf.open(pdfs[0]) as f:
                        payload = f.read()
                    # Forca extensao .pdf no nome (mesmo se chamador passou .zip ou outra)
                    base = Path(nome_destino).stem
                    nome_arquivo = f"{base}.pdf"
                    print(f"  > ZIP descompactado: {pdfs[0]} ({len(payload)/1024:.1f} KB)")
                else:
                    # ZIP sem PDF dentro - salva o ZIP bruto com extensao .zip
                    base = Path(nome_destino).stem
                    nome_arquivo = f"{base}.zip"
                    print(f"  > ZIP sem PDF dentro: {zf.namelist()[:5]} - salvando ZIP")
        except zipfile.BadZipFile:
            print(f"  ! ZIP corrompido - salvando bruto")
        except Exception as e:
            print(f"  ! erro descompactando: {type(e).__name__}: {e} - salvando bruto")

    path = pasta_destino / nome_arquivo
    path.write_bytes(payload)
    return path
