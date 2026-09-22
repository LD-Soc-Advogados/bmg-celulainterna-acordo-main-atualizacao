"""
Tarefa 4 - Advogados da parte Autora via API publica do DJEN/Comunica PJE.

API: https://comunicaapi.pje.jus.br/api/v1/comunicacao
     (swagger: https://comunicaapi.pje.jus.br/swagger/index.html)

Por CNJ, a API retorna as comunicacoes (intimacoes/citacoes) publicadas
no DJEN. Cada comunicacao traz:
  - destinatarios[]: {nome, polo}  -> polo "A" = Autora, "P" = Passivo
  - destinatarioadvogados[]: {advogado: {nome, numero_oab, uf_oab}}

Estrategia:
  1. Busca todas as comunicacoes do CNJ (paginado).
  2. Filtra as enderecadas SO ao polo Ativo (TODOS os destinatarios com
     polo A). Comunicacoes mistas (Autor + Reu juntos) sao descartadas:
     nelas os advogados dos DOIS lados aparecem misturados e nao da pra
     separar com seguranca.
  3. Extrai os advogados APENAS da comunicacao MAIS RECENTE do polo
     Ativo que tenha advogado listado - e o advogado ATUAL da causa.
     Comunicacoes antigas podem listar advogados que ja sairam do caso
     (substabelecimento, renuncia), entao nao agregamos historico.
  4. Blacklist dos socios LD (defensivo - eles representam o Banco, nunca
     a parte Autora; se aparecerem e porque o polo veio trocado).

NAO usa Edge/CDP - puro HTTP via curl_cffi (mesmo fingerprint Chrome do
zeev_download; a API recusa clients HTTP genericos com 403).

Validado em 12/06/2026 com o CNJ da CIV1579287: 8 comunicacoes, 4
advogados do polo A com nome + OAB/MG corretos.
"""
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from curl_cffi import requests as cffi_requests

from core.blacklist import BLACKLIST_PATH, carregar_blacklist, esta_no_bmg
from core.processo import extrair_estado_do_cnj
from core.relatorio import (
    ResultadoLinha,
    STATUS_OK,
    STATUS_ERRO,
    STATUS_JA_FECHADO,
)


NOME_FLUXO = "Consultar advogados DJEN"

API_URL = "https://comunicaapi.pje.jus.br/api/v1/comunicacao"
ITENS_POR_PAGINA = 100
MAX_PAGINAS = 10          # backstop: 1000 comunicacoes por processo
TIMEOUT_S = 30
PAUSA_ENTRE_PAGINAS_S = 0.4

# Blacklist unica do projeto (core/blacklist.py + blacklist_advogados.txt).
# Cache no import; qualquer edicao pela GUI so entra na proxima execucao.
BLACKLIST_LD = carregar_blacklist()


# Lista de Agressores - advogados que o escritorio quer SINALIZAR (nao
# exclui; so marca a coluna "Na_Lista_Agressores=SIM" na saida do DJEN).
# Mesmo formato da blacklist: NOME | OAB | UF. Editavel pela GUI.
AGRESSORES_PATH = Path(__file__).resolve().parent.parent / "lista_agressores.txt"


def carregar_agressores():
    """Le lista_agressores.txt -> list[(nome_parcial_upper, oab_digitos)].
    Vazia se o arquivo nao existir."""
    if not AGRESSORES_PATH.exists():
        return []
    entradas = []
    try:
        for linha in AGRESSORES_PATH.read_text(encoding="utf-8").splitlines():
            s = linha.strip()
            if not s or s.startswith("#"):
                continue
            partes = [p.strip() for p in s.split("|")]
            nome = partes[0].upper() if partes else ""
            oab = re.sub(r"[^0-9]", "", partes[1]) if len(partes) > 1 else ""
            if nome or oab:
                entradas.append((nome, oab))
    except Exception:
        return []
    return entradas


AGRESSORES = carregar_agressores()

# Identifica a PARTE do Banco pelo nome (o cliente eh sempre BMG; Cifra eh
# do grupo BMG). Usado pra mapear partes<->advogados por posicao, SEM
# blacklist por frequencia (que descartaria advogado do Autor com muitos
# processos contra o BMG - feedback do operador em 17/06).
_RE_BANCO = re.compile(r"\bBMG\b|\bCIFRA\b|BANCO", re.IGNORECASE)


def _eh_parte_banco(nome: str) -> bool:
    return bool(_RE_BANCO.search(nome or ""))


@dataclass
class AdvogadoDJEN:
    nome: str
    oab: str
    uf_oab: str
    ultima_comunicacao: str = ""   # data_disponibilizacao mais recente
    tribunal: str = ""
    n_comunicacoes: int = 0


@dataclass
class LinhaDJEN:
    """Uma CIV com seus advogados do polo Ativo."""
    civ: str
    cliente: str
    cnj: str
    estado: str
    advogados: list = field(default_factory=list)  # list[AdvogadoDJEN]
    capturado_em: str = ""


def _so_digitos(cnj: str) -> str:
    return re.sub(r"[^\d]", "", cnj or "")


def _sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c))


def _na_lista(nome: str, oab: str, lista) -> bool:
    """True se (nome parcial OU oab) casa alguma entrada da lista.
    Compara SEM acento ('ANTONIO' casa 'ANTÔNIO'). Usado so pra AGRESSORES;
    pra a BLACKLIST_LD, use esta_no_bmg() de core.blacklist."""
    nome_up = _sem_acento(nome).upper()
    oab_digits = re.sub(r"[^\d]", "", oab or "")
    for item_nome, item_oab in lista:
        if item_nome and _sem_acento(item_nome).upper() in nome_up:
            return True
        if item_oab and oab_digits == item_oab:
            return True
    return False


def _na_blacklist(nome: str, oab: str) -> bool:
    return esta_no_bmg(nome, oab, BLACKLIST_LD)


def eh_agressor(nome: str, oab: str) -> bool:
    """True se o advogado consta na Lista de Agressores do escritorio."""
    return _na_lista(nome, oab, AGRESSORES)


def _criar_sessao_djen():
    """Sessao curl_cffi com fingerprint Chrome (API recusa clients
    genericos com 403 - mesmo motivo do Zeev)."""
    sess = cffi_requests.Session(impersonate="chrome")
    return sess


_HTTP_RETRY = {429, 500, 502, 503, 504}  # transientes -> vale repetir


def _get_com_retry(sess, params, tentativas=5):
    """GET na API DJEN com retry. Repete tanto em timeout/erro de rede
    quanto quando a API responde com HTTP transiente (429/5xx) - a API
    publica do CNJ devolve esses codigos sob carga. Backoff crescente.
    Retorna o ultimo response (mesmo se ruim) ou levanta a ultima excecao
    se nem chegou a responder."""
    ultimo_erro = None
    ultimo_resp = None
    for t in range(tentativas):
        try:
            r = sess.get(API_URL, params=params, timeout=TIMEOUT_S)
            ultimo_resp = r
            if r.status_code in _HTTP_RETRY:
                time.sleep(2.0 * (t + 1))  # 2s, 4s, 6s...
                continue
            return r
        except Exception as e:
            ultimo_erro = e
            time.sleep(2.0 * (t + 1))
    if ultimo_resp is not None:
        return ultimo_resp     # devolve o ultimo (ex.: 500) pro chamador tratar
    raise ultimo_erro


def _buscar_comunicacoes(sess, cnj_digits: str, civ: str = "") -> list:
    """Busca TODAS as comunicacoes do processo (paginado).
    Retorna lista de items crus da API. Levanta RuntimeError em erro HTTP."""
    items = []
    pagina = 1
    while pagina <= MAX_PAGINAS:
        r = _get_com_retry(sess, {
            "numeroProcesso": cnj_digits,
            "itensPorPagina": ITENS_POR_PAGINA,
            "pagina": pagina,
        })
        if r.status_code != 200:
            raise RuntimeError(f"API DJEN respondeu HTTP {r.status_code}")
        data = r.json()
        lote = data.get("items", []) or []
        items.extend(lote)
        total = data.get("count", 0) or 0
        if len(items) >= total or not lote:
            break
        pagina += 1
        time.sleep(PAUSA_ENTRE_PAGINAS_S)
    return items


def _data_da_comunicacao(it: dict) -> str:
    return (it.get("data_disponibilizacao")
            or it.get("datadisponibilizacao") or "")


def _extrair_advogados_polo_ativo(items: list, civ: str = "") -> list:
    """Extrai os advogados ATUAIS da parte Autora.

    Regras (alinhadas com o time juridico em 06/2026):
      - So considera comunicacoes onde TODOS os destinatarios tem
        polo == 'A'. Comunicacoes mistas (Autor + Reu) listam advogados
        dos dois lados sem distincao - impossivel separar com seguranca.
      - Extrai APENAS da comunicacao MAIS RECENTE que tenha advogado
        valido listado. Comunicacoes antigas podem trazer advogado que
        ja saiu do caso; a mais nova reflete a representacao atual.
      - Se a mais recente nao listar advogado, cai pra proxima mais
        recente (e assim por diante).

    n_comunicacoes = em quantas comunicacoes do polo A aquele advogado
    aparece no historico inteiro (contexto: quanto maior, mais
    consolidado ele e na causa).
    """
    # 1) Filtra comunicacoes 100%% polo Ativo, mais recentes primeiro
    comms_a = []
    for it in items:
        destinatarios = it.get("destinatarios") or []
        if not destinatarios:
            continue
        if not all((d.get("polo") or "").upper() == "A" for d in destinatarios):
            continue
        comms_a.append(it)
    comms_a.sort(key=_data_da_comunicacao, reverse=True)

    if not comms_a:
        return []

    def _advogados_validos(it):
        """Advogados (nome, oab, uf) da comunicacao, sem blacklist/vazios."""
        out = []
        for da in (it.get("destinatarioadvogados") or []):
            adv = da.get("advogado") or {}
            nome = (adv.get("nome") or "").strip()
            oab = str(adv.get("numero_oab") or "").strip()
            uf = (adv.get("uf_oab") or "").strip().upper()
            if not nome or _na_blacklist(nome, oab):
                continue
            out.append((nome, oab, uf))
        return out

    # 2) Contagem de aparicoes no historico (so contexto, nao selecao)
    contagem = {}
    for it in comms_a:
        for nome, oab, uf in _advogados_validos(it):
            chave = (oab or nome.upper(), uf)
            contagem[chave] = contagem.get(chave, 0) + 1

    # 3) Pega a comunicacao MAIS RECENTE com advogado valido
    for it in comms_a:
        validos = _advogados_validos(it)
        if not validos:
            continue
        data_disp = _data_da_comunicacao(it)
        tribunal = it.get("siglaTribunal") or ""
        advs, vistos = [], set()
        for nome, oab, uf in validos:
            chave = (oab or nome.upper(), uf)
            if chave in vistos:
                continue
            vistos.add(chave)
            advs.append(AdvogadoDJEN(
                nome=nome, oab=oab, uf_oab=uf,
                ultima_comunicacao=data_disp, tribunal=tribunal,
                n_comunicacoes=contagem.get(chave, 1),
            ))
        n_antigas = len(comms_a) - 1
        if n_antigas > 0:
            print(f"  [{civ}] DJEN: extraido da comunicacao mais recente "
                  f"({data_disp[:10]}); {n_antigas} comunicacao(oes) antiga(s) ignorada(s)")
        return advs

    return []


# Status retornados pelo decisor (mapeiam pro saida_djen)
R_ENCONTRADO = "ENCONTRADO"      # comunicacao exclusiva do Autor (alta confianca)
R_INFERIDO = "INFERIDO"          # removidos advogados do Banco confirmados (conferir)
R_INCERTO = "INCERTO"            # tem advogados mas nao separou Autor/Banco
R_DEFENSORIA = "DEFENSORIA"      # autor assistido pela Defensoria (sem adv particular)
R_NAO_ENCONTRADO = "NAO ENCONTRADO"
R_ERRO = "ERRO"


def _key_adv(a) -> tuple:
    """Chave de identidade de um advogado: (oab_digitos|nome, uf)."""
    return (re.sub(r"[^0-9]", "", a.oab) or a.nome.upper(), a.uf_oab)


def _eh_banco_adv(a, bank_global) -> bool:
    """True se o advogado eh do Banco: na blacklist (lista conhecida) OU
    confirmado em comunicacao exclusiva do Banco (bank_global)."""
    return _na_blacklist(a.nome, a.oab) or _key_adv(a) in (bank_global or set())


def _advs_ordenados(it, freq=None):
    """Advogados de uma comunicacao, ordenados pelo `id` do vinculo.
    Retorna TODOS (a remocao de banco/blacklist eh feita no decidir_status,
    pra que remover um advogado do Banco conte como 'inferencia')."""
    advs = sorted(it.get("destinatarioadvogados") or [],
                  key=lambda a: a.get("id", 0))
    data_disp = _data_da_comunicacao(it)
    tribunal = it.get("siglaTribunal") or ""
    out, vistos = [], set()
    for da in advs:
        adv = da.get("advogado") or {}
        nome = (adv.get("nome") or "").strip()
        oab = str(adv.get("numero_oab") or "").strip()
        uf = (adv.get("uf_oab") or "").strip().upper()
        if not nome:
            continue
        k = (re.sub(r"[^0-9]", "", oab) or nome.upper(), uf)
        if k in vistos:
            continue
        vistos.add(k)
        ncom = freq.get(k, 1) if freq else 1
        out.append(AdvogadoDJEN(nome=nome, oab=oab, uf_oab=uf,
                                ultima_comunicacao=data_disp, tribunal=tribunal,
                                n_comunicacoes=ncom))
    return out


def coletar_djen(sess, civ: str, cnj: str) -> dict:
    """PASSO 1 - busca as comunicacoes de UM processo e extrai os sinais
    pro decisor. NAO decide o status (isso fica no decidir_status, que usa
    o conjunto de advogados do Banco confirmado no LOTE inteiro).

    Retorna dict:
      erro            - mensagem se a API falhou
      n_comunicacoes  - total
      author_lawyers  - list[AdvogadoDJEN] da comunicacao MAIS RECENTE
                        exclusiva do Autor (polo A e nenhuma parte do Banco)
      bank_local      - set de chaves (_key_adv) de advogados vistos em
                        comunicacao EXCLUSIVA do Banco (1+ partes, todas do
                        Banco pelo nome). PROVA de que sao do Banco.
      mixed_lawyers   - list[AdvogadoDJEN] da comunicacao MAIS RECENTE que
                        tem parte do Banco E parte nao-Banco (ou, na falta,
                        a comunicacao mais recente com advogado).
    """
    out = {"erro": "", "n_comunicacoes": 0, "author_lawyers": [],
           "bank_local": set(), "mixed_lawyers": [], "defensoria": False}

    cnj_digits = _so_digitos(cnj)
    if len(cnj_digits) < 14:
        out["erro"] = f"CNJ ausente/curto demais: '{cnj}'"
        return out
    try:
        items = _buscar_comunicacoes(sess, cnj_digits, civ)
    except Exception as e:
        out["erro"] = f"Falha na API DJEN: {type(e).__name__}: {e}"
        return out

    out["n_comunicacoes"] = len(items)
    if not items:
        return out

    # Defensoria Publica: se aparece como parte ou no texto, o autor
    # costuma ser assistido por ela (sem advogado particular pra acordo).
    # Ideia portada do modulo do Leandro.
    for it in items:
        blob = (it.get("texto") or "") + " " + \
               " ".join(str(d.get("nome") or "") for d in (it.get("destinatarios") or []))
        if "DEFENSORIA" in _sem_acento(blob).upper():
            out["defensoria"] = True
            break

    freq = {}
    for it in items:
        for da in (it.get("destinatarioadvogados") or []):
            adv = da.get("advogado") or {}
            oab = str(adv.get("numero_oab") or "").strip()
            uf = (adv.get("uf_oab") or "").strip().upper()
            nome = (adv.get("nome") or "").strip()
            if nome:
                k = (re.sub(r"[^0-9]", "", oab) or nome.upper(), uf)
                freq[k] = freq.get(k, 0) + 1

    def _dest(it):
        return it.get("destinatarios") or []

    recentes = sorted(items, key=_data_da_comunicacao, reverse=True)

    # bank_local: comunicacao EXCLUSIVA do Banco (todas as partes sao Banco
    # pelo nome) -> seus advogados sao PROVADAMENTE do Banco.
    for it in items:
        dest = _dest(it)
        if dest and all(_eh_parte_banco(d.get("nome")) for d in dest):
            for a in _advs_ordenados(it, freq):
                out["bank_local"].add(_key_adv(a))

    # author_lawyers: comunicacao exclusiva do Autor mais recente.
    for it in recentes:
        dest = _dest(it)
        if dest and all((d.get("polo") or "").upper() == "A"
                        and not _eh_parte_banco(d.get("nome")) for d in dest):
            advs = _advs_ordenados(it, freq)
            if advs:
                out["author_lawyers"] = advs
                break

    # mixed_lawyers: comunicacao mais recente com Banco + nao-Banco.
    for it in recentes:
        dest = _dest(it)
        if len(dest) < 2:
            continue
        if (any(_eh_parte_banco(d.get("nome")) for d in dest)
                and any(not _eh_parte_banco(d.get("nome")) for d in dest)):
            advs = _advs_ordenados(it, freq)
            if advs:
                out["mixed_lawyers"] = advs
                break
    # fallback: qualquer comunicacao recente com advogado (pra INCERTO).
    if not out["mixed_lawyers"]:
        for it in recentes:
            advs = _advs_ordenados(it, freq)
            if advs:
                out["mixed_lawyers"] = advs
                break

    return out


def decidir_status(info: dict, bank_global: set) -> dict:
    """PASSO 2 - decide o status do Autor a partir dos sinais coletados +
    o conjunto de advogados do Banco confirmado no LOTE (bank_global).

    Retorna {status, advogados, motivo}. Ordem de confianca:
      ENCONTRADO  - comunicacao exclusiva do Autor.
      INFERIDO    - comunicacao mista da qual REMOVI advogado(s) do Banco
                    confirmado(s) em comunicacao exclusiva do Banco (prova
                    direta, NAO frequencia). O que sobra eh do Autor.
      INCERTO     - ha advogados mas nenhum sinal pra separar Autor/Banco.
                    Lista TODOS com aviso. (A Tarefa 5/CNA ignora.)
      DEFENSORIA  - autor assistido pela Defensoria Publica (sem advogado
                    particular pra negociar). So quando nao achou advogado.
      NAO ENCONTRADO - sem advogado / so advogados do Banco.
    """
    if info.get("erro"):
        return {"status": R_ERRO, "advogados": [], "motivo": info["erro"]}
    if info.get("n_comunicacoes", 0) == 0:
        return {"status": R_NAO_ENCONTRADO, "advogados": [],
                "motivo": "Nenhuma comunicacao no DJEN para esse CNJ"}

    # 1) ENCONTRADO - comunicacao so do Autor (tira advogados do Banco:
    #    blacklist + confirmados em comunicacao exclusiva do Banco)
    author = [a for a in info.get("author_lawyers", [])
              if not _eh_banco_adv(a, bank_global)]
    if author:
        return {"status": R_ENCONTRADO, "advogados": author, "motivo": ""}

    # 2) DEFENSORIA - sem advogado particular do Autor, mas a Defensoria
    #    aparece no processo (nao adianta procurar contato no CNA).
    if info.get("defensoria"):
        return {"status": R_DEFENSORIA, "advogados": [],
                "motivo": "Autor assistido pela Defensoria Publica (sem advogado particular)"}

    # 3) INFERIDO/INCERTO - comunicacao mista, removendo advogados do Banco
    #    (blacklist OU confirmados em comunicacao exclusiva do Banco).
    mixed = info.get("mixed_lawyers", [])
    if mixed:
        restantes = [a for a in mixed if not _eh_banco_adv(a, bank_global)]
        removidos = len(mixed) - len(restantes)
        if restantes and removidos > 0:
            return {"status": R_INFERIDO, "advogados": restantes,
                    "motivo": ("Removido(s) advogado(s) do Banco (blacklist/"
                               "comunicacao exclusiva do Banco) - CONFERIR")}
        if not restantes:
            return {"status": R_NAO_ENCONTRADO, "advogados": [],
                    "motivo": "So advogados do Banco no processo"}
        # removidos == 0: sem prova pra separar -> lista todos
        return {"status": R_INCERTO, "advogados": mixed,
                "motivo": ("Nao foi possivel separar Autor/Banco - "
                           "PODE INCLUIR ADVOGADOS DO BMG; conferir manualmente")}

    return {"status": R_NAO_ENCONTRADO, "advogados": [],
            "motivo": f"Sem advogado nas comunicacoes ({info['n_comunicacoes']} comms)"}


def resolver_advogados_autor(sess, civ: str, cnj: str) -> dict:
    """Conveniencia pra UMA CIV (modo --civ): coleta + decide usando so o
    proprio bank_local (sem o conhecimento cross-CIV do lote)."""
    info = coletar_djen(sess, civ, cnj)
    dec = decidir_status(info, info.get("bank_local", set()))
    dec["n_comunicacoes"] = info.get("n_comunicacoes", 0)
    dec["erro"] = info.get("erro", "")
    return dec


def processar_consulta_djen(civ: str, cliente: str, cnj: str, sess=None) -> tuple:
    """Consulta os advogados do polo Ativo de UMA CIV via DJEN.

    sess: sessao curl_cffi reutilizavel (criar UMA no main e passar);
          se None, cria uma descartavel.

    Retorna (ResultadoLinha, LinhaDJEN ou None).
    """
    estado = extrair_estado_do_cnj(cnj) if cnj else ""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _resultado(status: str, msg: str, linha=None):
        rl = ResultadoLinha(
            civ=civ, cliente=cliente, cnj=cnj, estado=estado,
            fluxo=NOME_FLUXO, status=status, mensagem=msg,
            timestamp=timestamp,
        )
        return (rl, linha)

    cnj_digits = _so_digitos(cnj)
    if len(cnj_digits) < 14:
        return _resultado(
            STATUS_ERRO,
            f"CNJ ausente/curto demais pra consulta DJEN: '{cnj}'",
        )

    if sess is None:
        sess = _criar_sessao_djen()

    try:
        items = _buscar_comunicacoes(sess, cnj_digits, civ)
    except RuntimeError as e:
        return _resultado(STATUS_ERRO, str(e))
    except Exception as e:
        return _resultado(STATUS_ERRO, f"Falha na API DJEN: {type(e).__name__}: {e}")

    if not items:
        return _resultado(
            STATUS_JA_FECHADO,
            "Nenhuma comunicacao DJEN pra esse CNJ (processo fora do PJe/DJEN?)",
        )

    advogados = _extrair_advogados_polo_ativo(items, civ)
    if not advogados:
        return _resultado(
            STATUS_JA_FECHADO,
            f"{len(items)} comunicacao(oes) achadas, mas nenhum advogado "
            f"de polo Ativo identificado",
        )

    resumo = "; ".join(
        f"{a.nome} (OAB/{a.uf_oab} {a.oab})" for a in advogados[:4]
    )
    if len(advogados) > 4:
        resumo += f" +{len(advogados) - 4}"

    linha = LinhaDJEN(
        civ=civ, cliente=cliente, cnj=cnj, estado=estado,
        advogados=advogados, capturado_em=timestamp,
    )
    return _resultado(
        STATUS_OK,
        f"{len(advogados)} advogado(s) polo Ativo: {resumo}",
        linha,
    )
