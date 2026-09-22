"""
Entrypoint do bmg-celulainterna-acordo.

Por CIV autorizada (planilha do Banco):
  1) Tarefa 1 - captura a mensagem 'PEDIDO DE PAGAMENTO' e grava em
     saida/mensagens_acordo.xlsx.
  2) Tarefa 2 - baixa em DOWNLOADS_DIR/<NOME_CLIENTE>/ todos os
     documentos alvo presentes (PETICAO INICIAL, CITACAO, INTIMACAO,
     COPIA INTEGRAL).

Cada tarefa registra UMA linha no controle.xlsx (status independente).
As duas falham/sucedem de forma independente: erro na Tarefa 1 nao
impede a Tarefa 2 (e vice-versa) na MESMA CIV.

Uso:
  1. Planilha do Banco em 'Planilha Envio de casos*.xlsx' na raiz
     (ou aponta outra via --planilha).
  2. Roda 'abrir-edge-bmg.bat' e loga no e-Xyon (manual).
  3. Em PowerShell: '.venv\\Scripts\\python.exe main.py'

Flags:
  --planilha CAMINHO       Planilha alternativa.
  --limite N               Processa so as N primeiras CIVs.
  --civ CIVxxxx            Processa SO essa CIV (modo teste, pula planilha).
  --so-mensagem            Roda apenas a Tarefa 1 (pula downloads).
  --so-downloads           Roda apenas a Tarefa 2 (pula mensagem).
"""
import argparse
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))


# ============================================================
# LOGGING PERSISTENTE
# ============================================================
# Cada execucao gera um arquivo em logs/AAAA-MM-DD_HH-MM.log com TODA a
# saida (stdout + stderr). Sem isso, debug de erros em execucoes remotas
# (ex.: PC da Andreza) eh impossivel.

class _Tee:
    """File-like que escreve em N streams simultaneamente. Usado pra
    duplicar stdout pro console E pro arquivo de log."""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self):
        # Algumas libs (tqdm, etc.) chamam isatty(). Delega pro primeiro stream.
        try:
            return self.streams[0].isatty()
        except Exception:
            return False


def _iniciar_log_arquivo() -> Path:
    """Cria logs/AAAA-MM-DD_HH-MM.log e duplica stdout/stderr pra la.
    Retorna o path do arquivo de log. Idempotente: se ja chamado, retorna
    o stream atual (nao duplica de novo)."""
    ROOT = Path(__file__).resolve().parent
    pasta_logs = ROOT / "logs"
    pasta_logs.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = pasta_logs / f"{timestamp}.log"

    log_file = open(log_path, "w", encoding="utf-8", buffering=1)
    # buffering=1 = line-buffered. Garante que o log aparece no arquivo
    # quase em tempo real (importante se o processo morrer cedo).

    # Guarda referencias pros streams originais (pra GUI conseguir
    # restaurar se precisar).
    if not hasattr(sys, "_stdout_orig"):
        sys._stdout_orig = sys.stdout
        sys._stderr_orig = sys.stderr

    sys.stdout = _Tee(sys._stdout_orig, log_file)
    sys.stderr = _Tee(sys._stderr_orig, log_file)
    return log_path

from core import VERSAO
from core.config import (
    PLANILHA_INPUT, CONTROLE_XLSX, SAIDA_PLANILHA, DOWNLOADS_DIR,
    get_saida_mensagens_atual,
)
from core.browser import conectar_edge_cdp, aplicar_stealth_ao_contexto
from core.navegacao import (
    existe_campo_senha,
    aguardar_login_manual,
    estamos_em_unidade,
    clicar_entrar_unidade,
    clicar_juridico,
    clicar_pesquisa_causa,
    sessao_expirada,
)
from core.busca import (
    buscar_civ_exato,
    clicar_cliente_e_capturar_nome,
    normalizar_civ,
)
from core.processo import abrir_processo_do_cliente
from core.planilha_entrada import ler_planilha_casos, CasoAutorizado
from core.relatorio import (
    ResultadoLinha,
    STATUS_ERRO,
    STATUS_OK,
    STATUS_JA_FECHADO,
    registrar,
    escrever_relatorio_lote,
)
from core.zeev_download import criar_sessao_zeev, tentar_gerar_uuid_via_http

from fluxos.capturar_mensagem import (
    NOME_FLUXO as FLUXO_MENSAGEM,
    processar_captura,
    registrar_se_ok,
)
from fluxos.baixar_documentos import (
    NOME_FLUXO as FLUXO_DOWNLOADS,
    processar_downloads,
)
from fluxos.extrair_contatos import (
    NOME_FLUXO as FLUXO_EXTRACAO,
    processar_extracao,
    registrar_se_extraido,
)
from core.saida_contatos import ja_extraido as ja_extraido_contato


def _voltar_pra_busca(page):
    """Volta pra tela de Pesquisa Causa pro proximo CIV."""
    try:
        clicar_pesquisa_causa(page)
        time.sleep(1.0)
    except Exception as e:
        print(f"  (aviso) nao consegui voltar pra Pesquisa Causa: {e}")


def _erro(fluxo: str, caso: CasoAutorizado, mensagem: str) -> ResultadoLinha:
    """ResultadoLinha de ERRO antes do fluxo rodar (ex.: busca falhou)."""
    return ResultadoLinha(
        civ=caso.civ,
        cliente=caso.cliente,
        cnj=caso.cnj,
        estado="",
        fluxo=fluxo,
        status=STATUS_ERRO,
        mensagem=mensagem,
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def main():
    parser = argparse.ArgumentParser(
        description="Captura mensagem 'PEDIDO DE PAGAMENTO' + baixa documentos do BMG",
    )
    parser.add_argument(
        "--planilha", default=None,
        help="Caminho de planilha alternativa (default: Planilha Envio de casos*.xlsx na raiz).",
    )
    parser.add_argument(
        "--limite", type=int, default=None,
        help="Processa so as N primeiras CIVs (util pra teste).",
    )
    parser.add_argument(
        "--civ", default=None,
        help="Processa SO essa CIV especifica, ignorando a planilha. Ex.: --civ CIV1387277",
    )
    parser.add_argument(
        "--so-mensagem", action="store_true",
        help="Roda apenas a Tarefa 1 (extrai dados da aba Financeiro); pula downloads.",
    )
    parser.add_argument(
        "--so-downloads", action="store_true",
        help="Roda apenas a Tarefa 2 (download de documentos); pula a Tarefa 1.",
    )
    parser.add_argument(
        "--so-extracao", action="store_true",
        help=("Roda apenas a Tarefa 3 (extracao de contatos do advogado) "
              "lendo PDFs ja baixados em DOWNLOADS_DIR. NAO abre Edge / CDP."),
    )
    parser.add_argument(
        "--so-djen", action="store_true",
        help=("Roda apenas a Tarefa 4 (advogados + OAB do polo Ativo via "
              "API publica do DJEN/Comunica PJE). NAO precisa de Edge."),
    )
    parser.add_argument(
        "--so-cna", action="store_true",
        help=("Roda apenas a Tarefa 5 (enriquece advogados com Telefone/"
              "Endereco via CNA cna.oab.org.br). Le a saida da Tarefa 4 "
              "(advogados_djen_*.xlsx). Precisa do Edge dedicado aberto. "
              "RETOMA automaticamente: quem ja tem resultado definitivo "
              "numa execucao anterior (Status_CNA) nao e reconsultado - "
              "so quem ficou PENDENTE (captcha/rate-limit/timeout). Use "
              "--forcar pra reconsultar todo mundo do zero."),
    )
    parser.add_argument(
        "--djen-xlsx", default=None,
        help=("Caminho da planilha advogados_djen_*.xlsx pra Tarefa 5. "
              "Se omitido, usa a mais recente em saida/."),
    )
    parser.add_argument(
        "--so-manifesto", action="store_true",
        help=("Gera SO o manifesto da planilha (CIV/Cliente/CNJ/Pasta de "
              "download) em saida/manifesto_ultima_rodada_*.xlsx. Nao abre "
              "Edge. Util pra rodar o Cowork nas pastas baixadas."),
    )
    parser.add_argument(
        "--completo", action="store_true",
        help=("PIPELINE COMPLETO: roda Tarefa 1 + Tarefa 2 e, ao terminar, "
              "encadeia Tarefa 4 (DJEN) e Tarefa 5 (CNA) automaticamente. "
              "O operador nao precisa rodar cada uma na mao."),
    )
    parser.add_argument(
        "--forcar", action="store_true",
        help=("Pula idempotencia/retomada: na Tarefa 1, reprocessa a CIV "
              "mesmo se ja foi capturada em rodada anterior (util depois "
              "de mudancas no schema, ex.: colunas novas da aba "
              "Financeiro); na Tarefa 5 (CNA), ignora o cache de "
              "resultados anteriores e reconsulta TODO MUNDO no CNA do "
              "zero, mesmo quem ja tinha Status_CNA definitivo."),
    )
    args = parser.parse_args()

    n_so = sum([args.so_mensagem, args.so_downloads, args.so_extracao,
                args.so_djen, args.so_cna, args.so_manifesto])
    if n_so > 1:
        print("ERRO: --so-mensagem, --so-downloads, --so-extracao, "
              "--so-djen, --so-cna e --so-manifesto sao mutuamente exclusivos.")
        return 1
    if args.completo and n_so > 0:
        print("ERRO: --completo ja roda tudo (T1+T2+DJEN+CNA); nao combine "
              "com as flags --so-*.")
        return 1

    # Logging persistente: a partir daqui, tudo que sai no console vai
    # tambem pra logs/AAAA-MM-DD_HH-MM-SS.log. Critico pra debug remoto.
    log_path = _iniciar_log_arquivo()
    print(f">>> Log desta execucao: {log_path}")

    # Tarefa 3 (extracao) eh standalone - rodada com main_extracao()
    if args.so_extracao:
        return main_extracao(args)

    # Tarefa 4 (DJEN) - standalone, puro HTTP, sem Edge/CDP.
    if args.so_djen:
        return main_djen(args)

    # Tarefa 5 (CNA) - standalone, usa Edge CDP (recaptcha exige browser).
    if args.so_cna:
        return main_cna(args)

    # Manifesto standalone - so gera o relatorio, sem browser.
    if args.so_manifesto:
        return main_manifesto(args)

    rodar_mensagem = not args.so_downloads
    rodar_downloads = not args.so_mensagem

    print(f">>> {VERSAO} <<<")
    tarefas_str = []
    if rodar_mensagem:
        tarefas_str.append("Tarefa 1 (Financeiro)")
    if rodar_downloads:
        tarefas_str.append("Tarefa 2 (downloads)")
    print(f">>> Executando: {' + '.join(tarefas_str)}")

    if args.civ:
        civ_canon, civ_num = normalizar_civ(args.civ)
        casos = [CasoAutorizado(
            civ=civ_canon, civ_num=civ_num,
            cliente="", cnj="", recurso="", uf="",
            responsavel="", observacoes="",
        )]
        print(f">>> Modo teste: processando so {civ_canon} (pula planilha)")
    else:
        caminho = Path(args.planilha) if args.planilha else PLANILHA_INPUT
        print(f">>> Planilha: {caminho}")
        try:
            casos = ler_planilha_casos(caminho)
        except FileNotFoundError as e:
            print(f"\nERRO: {e}")
            print("Coloque a planilha 'Planilha Envio de casos*.xlsx' na raiz,")
            print("passe --planilha CAMINHO, ou use --civ CIVxxx pra teste isolado.")
            return 1
        except ValueError as e:
            print(f"\nERRO na planilha: {e}")
            return 1

    if not casos:
        print("\nNenhum caso na planilha. Nada a fazer.")
        return 0

    if args.limite:
        casos = casos[: args.limite]

    # Planilha de Financeiro DA RODADA ATUAL - timestamp evita acumular.
    # Idempotencia continua funcionando: ja_capturado_em_pasta() varre
    # TODOS os financeiro_*.xlsx da pasta saida/.
    saida_mensagens_atual = get_saida_mensagens_atual() if rodar_mensagem else None

    print(f">>> {len(casos)} caso(s) pra processar.")
    if rodar_mensagem:
        print(f">>> Planilha de Financeiro (NOVA): {saida_mensagens_atual.name}")
    if rodar_downloads:
        print(f">>> Pasta de downloads:          {DOWNLOADS_DIR}")
    print(f">>> Controle:                    {CONTROLE_XLSX}\n")

    inicio_lote = datetime.now()
    resultados = []

    with sync_playwright() as p:
        try:
            browser, context, page = conectar_edge_cdp(p)
        except RuntimeError as e:
            print(f"\nERRO de conexao: {e}")
            return 1

        try:
            if existe_campo_senha(page):
                logado = aguardar_login_manual(page, timeout_total_seg=300)
                if not logado:
                    print("Timeout aguardando login. Encerrando.")
                    return 1

            if estamos_em_unidade(page):
                print(">>> Tela de unidade detectada — clicando Entrar.")
                clicar_entrar_unidade(page)
                time.sleep(1.5)

            if not clicar_juridico(page):
                print("ERRO: nao achei o menu Juridico.")
                return 1
            time.sleep(1.0)
            if not clicar_pesquisa_causa(page):
                print("ERRO: nao achei Pesquisa Causa no submenu.")
                return 1
            time.sleep(1.5)

            # Cria sessao Zeev UMA vez antes do loop (reuso entre CIVs).
            # Se nao houver cookie ConvergeSilentLogin_*, tenta gerar via
            # SilentRedirect/SilentAccess HTTP. Se isso tambem falhar,
            # o operador precisa clicar em UM disquete manualmente no
            # Edge dedicado e rodar de novo.
            sess_zeev, uuid_zeev = None, None
            if rodar_downloads:
                print(">>> Inicializando sessao Zeev...")
                sess_zeev, uuid_zeev = criar_sessao_zeev(context)
                if uuid_zeev is None:
                    print("  (sem cookie ConvergeSilentLogin - tentando gerar via HTTP)")
                    uuid_zeev = tentar_gerar_uuid_via_http(sess_zeev)
                if uuid_zeev:
                    print(f"  Sessao Zeev OK (UUID: {uuid_zeev[:8]}...{uuid_zeev[-4:]})")
                else:
                    print("  AVISO: sem UUID Zeev. Tarefa 2 (downloads) vai falhar.")
                    print("  Solucao: no Edge dedicado, abra um documento manualmente")
                    print("  UMA vez (qualquer disquete), e rode o robo de novo.")

            # ============================================================
            # LOOP PRINCIPAL
            # ============================================================
            for idx, caso in enumerate(casos, 1):
                civ = caso.civ
                print(f"\n[{idx}/{len(casos)}] {civ} — {caso.cliente[:40]}")

                try:
                    _ = page.url
                except Exception as e:
                    print(f"\n!!! ERRO: pagina/contexto fechado ({type(e).__name__}: {e}).")
                    print(">>> Abortando lote. Reabra o Edge via abrir-edge-bmg.bat,")
                    print(">>> verifique que ainda esta logado, e rode de novo.")
                    print(f">>> Processados ate aqui: {idx-1}/{len(casos)}")
                    break

                # Detecta sessao expirada (e-Xyon desloga apos ~20min idle).
                # Aborta limpo em vez de gerar TargetClosedError em cascata.
                if sessao_expirada(page):
                    print(f"\n!!! SESSAO EXPIRADA: e-Xyon deslogou (provavel idle > 20min).")
                    print(">>> Abortando lote agora pra nao gerar erros em cascata.")
                    print(">>> Solucao: faca login MANUAL no Edge dedicado e rode de novo.")
                    print(f">>> Processados ate aqui: {idx-1}/{len(casos)}.")
                    print(f">>> Idempotencia vai pular tudo que ja foi feito; ele retoma daqui.")
                    break

                # Etapa comum: buscar CIV, abrir cliente, abrir processo
                pre_ok = False
                cliente_efetivo = caso.cliente
                cnj_efetivo = caso.cnj
                try:
                    if not buscar_civ_exato(page, civ):
                        msg = "Falha ao buscar CIV"
                        if rodar_mensagem:
                            resultados.append(_erro(FLUXO_MENSAGEM, caso, msg))
                        if rodar_downloads:
                            resultados.append(_erro(FLUXO_DOWNLOADS, caso, msg))
                    else:
                        nome = clicar_cliente_e_capturar_nome(page, timeout_seg=15)
                        if not nome:
                            msg = "Cliente nao encontrado na lista de resultados"
                            if rodar_mensagem:
                                resultados.append(_erro(FLUXO_MENSAGEM, caso, msg))
                            if rodar_downloads:
                                resultados.append(_erro(FLUXO_DOWNLOADS, caso, msg))
                        else:
                            ok_proc, cnj = abrir_processo_do_cliente(page, timeout_seg=15)
                            # cnj pode vir vazio (processo antigo / numero fora do
                            # padrao CNJ). Aceita: usa cnj da planilha como fallback.
                            if not ok_proc:
                                msg = "Falha ao abrir processo (clique em PROCESSO falhou)"
                                if rodar_mensagem:
                                    resultados.append(_erro(FLUXO_MENSAGEM, caso, msg))
                                if rodar_downloads:
                                    resultados.append(_erro(FLUXO_DOWNLOADS, caso, msg))
                            else:
                                cliente_efetivo = nome or caso.cliente
                                cnj_efetivo = cnj or caso.cnj
                                if caso.cliente and nome and caso.cliente.upper() not in nome.upper():
                                    print(f"  (aviso) cliente da planilha '{caso.cliente}' "
                                          f"!= capturado '{nome}'. Usando o capturado.")
                                pre_ok = True
                except Exception as exc:
                    msg = f"Excecao na navegacao: {type(exc).__name__}: {exc}"
                    if rodar_mensagem:
                        resultados.append(_erro(FLUXO_MENSAGEM, caso, msg))
                    if rodar_downloads:
                        resultados.append(_erro(FLUXO_DOWNLOADS, caso, msg))

                if not pre_ok:
                    # Pra cada erro pre-fluxo ja registrado, salva no controle.xlsx
                    # (os 2 ultimos da lista sao os dessa CIV)
                    novos = []
                    if rodar_mensagem and rodar_downloads:
                        novos = resultados[-2:]
                    elif rodar_mensagem or rodar_downloads:
                        novos = resultados[-1:]
                    for r in novos:
                        registrar(CONTROLE_XLSX, r)
                        print(f"  status[{r.fluxo}]: {r.status} — {r.mensagem}")
                    if idx < len(casos):
                        _voltar_pra_busca(page)
                        pausa = random.uniform(2.0, 3.0)
                        print(f"  ...pausando {pausa:.1f}s antes do proximo")
                        time.sleep(pausa)
                    continue

                # Tarefa 1: captura de mensagem
                if rodar_mensagem:
                    try:
                        resultado_msg, linha_msg = processar_captura(
                            page, civ, cliente_efetivo, cnj_efetivo,
                            forcar=args.forcar,
                        )
                        registrar_se_ok(linha_msg, saida_mensagens_atual)
                    except Exception as exc:
                        resultado_msg = _erro(
                            FLUXO_MENSAGEM, caso,
                            f"Excecao no fluxo: {type(exc).__name__}: {exc}",
                        )
                    print(f"  status[{FLUXO_MENSAGEM}]: {resultado_msg.status} — {resultado_msg.mensagem}")
                    resultados.append(resultado_msg)
                    registrar(CONTROLE_XLSX, resultado_msg)

                # Tarefa 2: download de documentos.
                # Retorna (ResultadoLinha, sess_zeev, uuid_zeev) -
                # sess/uuid podem ser atualizados via warmup automatico
                # na primeira CIV se nao tinhamos cookie no inicio.
                if rodar_downloads:
                    try:
                        resultado_dl, sess_zeev, uuid_zeev = processar_downloads(
                            page, civ, cliente_efetivo, cnj_efetivo,
                            sess_zeev, uuid_zeev,
                            context=context,
                        )
                    except Exception as exc:
                        resultado_dl = _erro(
                            FLUXO_DOWNLOADS, caso,
                            f"Excecao no fluxo: {type(exc).__name__}: {exc}",
                        )
                    print(f"  status[{FLUXO_DOWNLOADS}]: {resultado_dl.status} — {resultado_dl.mensagem}")
                    resultados.append(resultado_dl)
                    registrar(CONTROLE_XLSX, resultado_dl)

                if idx < len(casos):
                    _voltar_pra_busca(page)
                    pausa = random.uniform(2.0, 3.0)
                    print(f"  ...pausando {pausa:.1f}s antes do proximo")
                    time.sleep(pausa)

            # ============================================================
            # RESUMO + RELATORIO
            # ============================================================
            total = len(resultados)
            ok = sum(1 for r in resultados if r.status == STATUS_OK)
            erro = sum(1 for r in resultados if r.status == STATUS_ERRO)
            ja = sum(1 for r in resultados if r.status == STATUS_JA_FECHADO)

            print(f"\n{'=' * 72}")
            print(f"  RESUMO GERAL ({len(casos)} CIVs, {total} resultados)")
            print(f"{'=' * 72}")
            print(f"  [OK]         {ok}")
            print(f"  [JA_FECHADO] {ja}")
            print(f"  [ATENCAO]    {erro}")
            print(f"{'=' * 72}")
            for r in resultados:
                marca = "[ATENCAO]" if r.status == STATUS_ERRO else f"[{r.status}]"
                fluxo_curto = "FIN " if r.fluxo == FLUXO_MENSAGEM else "DOCS"
                print(f"  {marca:<12} {fluxo_curto} {r.civ:<14} {r.cliente[:25]:<25} {r.mensagem[:55]}")

            rel = escrever_relatorio_lote(
                pasta_destino=CONTROLE_XLSX.parent,
                fluxo="Extrair Financeiro + Download documentos",
                resultados=resultados,
                inicio=inicio_lote,
                instrucao_acao_manual=(
                    "Abrir a CIV manualmente no e-Xyon. Para o Financeiro: aba "
                    "Financeiro, ler o lancamento de pagamento mais recente. "
                    "Para documentos: aba Documentos, baixar PETICAO/CITACAO/"
                    "INTIMACAO/COPIA INTEGRAL que faltarem."
                ),
            )
            print(f"\n>>> Controle:               {CONTROLE_XLSX}")
            if rodar_mensagem:
                print(f">>> Planilha de Financeiro: {saida_mensagens_atual}")
            if rodar_downloads:
                print(f">>> Pasta de downloads:     {DOWNLOADS_DIR}")
            print(f">>> Relatorio do lote:      {rel}")

            if erro > 0:
                print(f"\n>>> ATENCAO: {erro} resultado(s) com ERRO. Detalhes no relatorio.")

        finally:
            try:
                browser.close()
            except Exception:
                pass

    # Manifesto da rodada (CIV/Cliente/CNJ/Pasta) - pro Cowork rodar so nas
    # pastas desta rodada. Gerado sempre ao fim do T1+T2.
    _mani = _gerar_manifesto_rodada(casos)
    if _mani:
        print(f">>> Manifesto da rodada:    {_mani}")

    # PIPELINE COMPLETO: encadeia Tarefa 4 (DJEN) e Tarefa 5 (CNA) depois
    # do T1+T2, pro operador nao precisar rodar cada uma na mao.
    if args.completo:
        print(f"\n{'#' * 72}")
        print("#  PIPELINE COMPLETO - seguindo para Tarefa 4 (advogados DJEN)")
        print(f"{'#' * 72}\n")
        try:
            main_djen(args)
        except Exception as exc:
            print(f">>> ERRO na Tarefa 4 (DJEN): {type(exc).__name__}: {exc}")

        print(f"\n{'#' * 72}")
        print("#  PIPELINE COMPLETO - seguindo para Tarefa 5 (contato CNA)")
        print(f"{'#' * 72}\n")
        try:
            main_cna(args)
        except Exception as exc:
            print(f">>> ERRO na Tarefa 5 (CNA): {type(exc).__name__}: {exc}")

        print(f"\n{'#' * 72}")
        print("#  PIPELINE COMPLETO FINALIZADO (T1 + T2 + DJEN + CNA)")
        print(f"{'#' * 72}")

    return 0


def _gerar_manifesto_rodada(casos) -> "Path":
    """Gera saida/manifesto_ultima_rodada_<ts>.xlsx pros `casos`. Retorna o
    Path ou None em caso de erro (nao levanta)."""
    from core.config import ROOT
    from core.manifesto import gerar_manifesto
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
    destino = ROOT / "saida" / f"manifesto_ultima_rodada_{ts}.xlsx"
    try:
        return gerar_manifesto(casos, DOWNLOADS_DIR, destino)
    except Exception as exc:
        print(f">>> (aviso) falha ao gerar manifesto: {type(exc).__name__}: {exc}")
        return None


def main_manifesto(args) -> int:
    """Gera SO o manifesto (CIV/Cliente/CNJ/Pasta de download) da planilha.
    Sem browser. Usado pra apontar o Cowork pras pastas da ultima rodada."""
    print(f">>> {VERSAO} <<<")
    print(">>> Gerando manifesto (CIV / Cliente / CNJ / Pasta de download)...")

    if args.civ:
        civ_canon, civ_num = normalizar_civ(args.civ)
        casos = [CasoAutorizado(civ=civ_canon, civ_num=civ_num, cliente="",
                                cnj="", recurso="", uf="", responsavel="",
                                observacoes="")]
    else:
        caminho = Path(args.planilha) if args.planilha else PLANILHA_INPUT
        print(f">>> Planilha: {caminho}")
        try:
            casos = ler_planilha_casos(caminho)
        except FileNotFoundError as e:
            print(f"\nERRO: {e}")
            return 1
        except ValueError as e:
            print(f"\nERRO na planilha: {e}")
            return 1

    if args.limite:
        casos = casos[: args.limite]
    if not casos:
        print("Nada a processar.")
        return 0

    destino = _gerar_manifesto_rodada(casos)
    if destino:
        print(f">>> Manifesto gerado ({len(casos)} CIVs): {destino}")
        return 0
    return 1


def main_extracao(args) -> int:
    """Tarefa 3 standalone - le PDFs ja baixados, extrai contatos do(s)
    advogado(s), gera saida/contatos_advogados.xlsx. NAO abre Edge / CDP.
    """
    from core.config import ROOT
    # Cada rodada gera planilha NOVA com timestamp - nao acumula com runs
    # antigos. A extracao eh barata (offline, le so PDFs), nao precisa
    # idempotencia. Operador compara/merge depois se quiser.
    timestamp_arq = datetime.now().strftime("%Y-%m-%d_%H-%M")
    SAIDA_CONTATOS = ROOT / "saida" / f"contatos_advogados_{timestamp_arq}.xlsx"

    print(f">>> {VERSAO} <<<")
    print(f">>> Tarefa 3 (standalone): {FLUXO_EXTRACAO}")
    print(f">>> Le PDFs em: {DOWNLOADS_DIR}")
    print(f">>> Saida:      {SAIDA_CONTATOS}\n")

    if args.civ:
        civ_canon, civ_num = normalizar_civ(args.civ)
        casos = [CasoAutorizado(
            civ=civ_canon, civ_num=civ_num,
            cliente="", cnj="", recurso="", uf="",
            responsavel="", observacoes="",
        )]
        print(f">>> Modo teste: processando so {civ_canon}")
        print(">>> (sem dados da planilha - cliente/CNJ ficam em branco)")
    else:
        caminho = Path(args.planilha) if args.planilha else PLANILHA_INPUT
        print(f">>> Planilha: {caminho}")
        try:
            casos = ler_planilha_casos(caminho)
        except FileNotFoundError as e:
            print(f"\nERRO: {e}")
            return 1
        except ValueError as e:
            print(f"\nERRO na planilha: {e}")
            return 1

    if args.limite:
        casos = casos[: args.limite]

    if not casos:
        print("Nada a processar.")
        return 0

    print(f">>> {len(casos)} caso(s) pra processar.\n")
    inicio_lote = datetime.now()
    resultados = []
    contas = {"OK": 0, "ERRO": 0, "JA_FECHADO": 0, "SEM_PASTA": 0}

    for idx, caso in enumerate(casos, 1):
        civ = caso.civ
        cliente = caso.cliente
        cnj = caso.cnj
        print(f"[{idx}/{len(casos)}] {civ} — {cliente[:40]}")

        # Sem idempotencia: cada rodada gera planilha datada nova; reprocessa
        # tudo do zero. Como a Tarefa 3 eh offline (PDFs ja baixados), o custo
        # de rodar tudo de novo eh segundos por CIV.

        try:
            resultado, linha_contato = processar_extracao(civ, cliente, cnj)
            registrar_se_extraido(linha_contato, SAIDA_CONTATOS)
        except Exception as exc:
            resultado = ResultadoLinha(
                civ=civ, cliente=cliente, cnj=cnj, estado="",
                fluxo=FLUXO_EXTRACAO, status=STATUS_ERRO,
                mensagem=f"Excecao: {type(exc).__name__}: {exc}",
                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )

        print(f"  status[{FLUXO_EXTRACAO}]: {resultado.status} — {resultado.mensagem}")
        resultados.append(resultado)
        registrar(CONTROLE_XLSX, resultado)
        contas[resultado.status] = contas.get(resultado.status, 0) + 1

    # Resumo
    print(f"\n{'=' * 72}")
    print(f"  RESUMO — {FLUXO_EXTRACAO}")
    print(f"{'=' * 72}")
    print(f"  Total processado: {len(resultados)}")
    print(f"  [OK]:         {contas.get('OK', 0)}")
    print(f"  [JA_FECHADO]: {contas.get('JA_FECHADO', 0)}")
    print(f"  [ERRO]:       {contas.get('ERRO', 0)}")
    print(f"{'=' * 72}")
    for r in resultados:
        marca = "[ATENCAO]" if r.status == STATUS_ERRO else f"[{r.status}]"
        print(f"  {marca:<12} {r.civ:<14} {r.cliente[:30]:<30} {r.mensagem[:55]}")

    rel = escrever_relatorio_lote(
        pasta_destino=CONTROLE_XLSX.parent,
        fluxo=FLUXO_EXTRACAO,
        resultados=resultados,
        inicio=inicio_lote,
        instrucao_acao_manual=(
            "Abrir o PDF da peticao_inicial manualmente e identificar o "
            "advogado do Autor (timbre, final do doc, ou procuracao se houver)."
        ),
    )
    print(f"\n>>> Controle:               {CONTROLE_XLSX}")
    print(f">>> Planilha de contatos:   {SAIDA_CONTATOS}")
    print(f">>> Relatorio do lote:      {rel}")

    return 0


def main_djen(args) -> int:
    """Tarefa 4 standalone - advogados + OAB do polo Ativo via API publica
    do DJEN/Comunica PJE (comunicaapi.pje.jus.br).

    100%% HTTP - NAO abre Edge / CDP / login. Pode rodar a qualquer hora,
    inclusive pra planilhas antigas.
    """
    from core.config import ROOT
    from core.processo import extrair_estado_do_cnj
    from fluxos.consultar_djen import (
        NOME_FLUXO as FLUXO_DJEN,
        coletar_djen, decidir_status,
        R_ENCONTRADO, R_INFERIDO, R_INCERTO, R_DEFENSORIA,
        R_NAO_ENCONTRADO, R_ERRO,
        _criar_sessao_djen,
    )
    from core.saida_djen import (
        registrar_djen,
        STATUS_ENCONTRADO, STATUS_INFERIDO, STATUS_INCERTO,
        STATUS_DEFENSORIA, STATUS_NAO_ENCONTRADO, STATUS_ERRO_DJEN,
    )

    timestamp_arq = datetime.now().strftime("%Y-%m-%d_%H-%M")
    SAIDA_DJEN = ROOT / "saida" / f"advogados_djen_{timestamp_arq}.xlsx"

    print(f">>> {VERSAO} <<<")
    print(f">>> Tarefa 4 (standalone): {FLUXO_DJEN}")
    print(f">>> API:   https://comunicaapi.pje.jus.br (sem Edge / sem login)")
    print(f">>> Saida: {SAIDA_DJEN}\n")

    if args.civ:
        civ_canon, civ_num = normalizar_civ(args.civ)
        casos = [CasoAutorizado(
            civ=civ_canon, civ_num=civ_num,
            cliente="", cnj="", recurso="", uf="",
            responsavel="", observacoes="",
        )]
        print(f">>> Modo teste: processando so {civ_canon}")
        print(">>> AVISO: --civ nao tem CNJ (a consulta DJEN exige CNJ);")
        print(">>>        use a planilha pro fluxo normal.")
    else:
        caminho = Path(args.planilha) if args.planilha else PLANILHA_INPUT
        print(f">>> Planilha: {caminho}")
        try:
            casos = ler_planilha_casos(caminho)
        except FileNotFoundError as e:
            print(f"\nERRO: {e}")
            return 1
        except ValueError as e:
            print(f"\nERRO na planilha: {e}")
            return 1

    if args.limite:
        casos = casos[: args.limite]

    if not casos:
        print("Nada a processar.")
        return 0

    print(f">>> {len(casos)} caso(s) pra processar.\n")
    inicio_lote = datetime.now()
    sess = _criar_sessao_djen()
    resultados = []
    contas = {"encontrado": 0, "inferido": 0, "incerto": 0,
              "defensoria": 0, "nao_encontrado": 0, "erro": 0}

    # Mapeia o status do decisor -> (status saida, status log/controle, contador)
    MAP = {
        R_ENCONTRADO:     (STATUS_ENCONTRADO,     STATUS_OK,         "encontrado"),
        R_INFERIDO:       (STATUS_INFERIDO,       STATUS_OK,         "inferido"),
        R_INCERTO:        (STATUS_INCERTO,        STATUS_OK,         "incerto"),
        R_DEFENSORIA:     (STATUS_DEFENSORIA,     STATUS_JA_FECHADO, "defensoria"),
        R_NAO_ENCONTRADO: (STATUS_NAO_ENCONTRADO, STATUS_JA_FECHADO, "nao_encontrado"),
        R_ERRO:           (STATUS_ERRO_DJEN,      STATUS_ERRO,       "erro"),
    }

    # ===================== PASSO 1: coleta + advogados do Banco ==============
    # Coleta as comunicacoes de cada CIV e acumula bank_global = advogados
    # PROVADAMENTE do Banco (vistos em comunicacao exclusiva do Banco em
    # QUALQUER CIV). Evidencia direta - nao eh frequencia.
    print(">>> Passo 1/2: coletando comunicacoes do DJEN...\n")
    coletas = {}
    bank_global = set()
    for idx, caso in enumerate(casos, 1):
        civ, cnj = caso.civ, caso.cnj
        print(f"[{idx}/{len(casos)}] {civ} (CNJ: {cnj or '(vazio)'})")
        try:
            info = coletar_djen(sess, civ, cnj)
        except Exception as exc:
            info = {"erro": f"Excecao: {type(exc).__name__}: {exc}",
                    "n_comunicacoes": 0, "author_lawyers": [],
                    "bank_local": set(), "mixed_lawyers": [], "defensoria": False}
        coletas[civ] = info
        bank_global |= info.get("bank_local", set())
        if idx < len(casos):
            time.sleep(random.uniform(0.6, 1.2))

    print(f"\n>>> Advogados do Banco confirmados (comunicacao exclusiva do "
          f"Banco): {len(bank_global)}")

    # ===================== PASSO 2: decide + grava ==========================
    print(">>> Passo 2/2: separando advogado do Autor e gravando...\n")
    for caso in casos:
        civ, cliente, cnj = caso.civ, caso.cliente, caso.cnj
        estado = extrair_estado_do_cnj(cnj) if cnj else ""
        info = coletas.get(civ, {})
        dec = decidir_status(info, bank_global)

        status_saida, status_log, chave = MAP.get(
            dec["status"], (STATUS_ERRO_DJEN, STATUS_ERRO, "erro"))
        advogados = dec.get("advogados", [])
        motivo = dec.get("motivo", "")
        contas[chave] += 1

        registrar_djen(SAIDA_DJEN, civ, cliente, cnj, estado,
                       advogados, status_saida, motivo)

        if advogados:
            nomes = "; ".join(f"{a.nome} (OAB/{a.uf_oab} {a.oab})"
                              for a in advogados[:3])
            prefixo = "" if dec["status"] == R_ENCONTRADO else f"[{dec['status']}] "
            msg = f"{prefixo}{len(advogados)} advogado(s): {nomes}"
        else:
            msg = motivo
        r = ResultadoLinha(civ=civ, cliente=cliente, cnj=cnj, estado=estado,
                           fluxo=FLUXO_DJEN, status=status_log, mensagem=msg,
                           timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        resultados.append(r)
        registrar(CONTROLE_XLSX, r)
        print(f"  {civ} {dec['status']}: {msg[:64]}")

    print(f"\n{'=' * 72}")
    print(f"  RESUMO — {FLUXO_DJEN}")
    print(f"{'=' * 72}")
    print(f"  Total de CIVs:        {len(resultados)}")
    print(f"  [ENCONTRADO]          {contas['encontrado']}")
    print(f"  [INFERIDO]            {contas['inferido']}")
    print(f"  [INCERTO]             {contas['incerto']}")
    print(f"  [DEFENSORIA]          {contas['defensoria']}")
    print(f"  [NAO ENCONTRADO]      {contas['nao_encontrado']}")
    print(f"  [ERRO]                {contas['erro']}")
    print(f"{'=' * 72}")
    for r in resultados:
        if r.status == STATUS_OK:
            marca = "[OK]"
        elif r.status == STATUS_ERRO:
            marca = "[ERRO]"
        else:
            marca = "[NAO ENCONTR]"
        print(f"  {marca:<13} {r.civ:<14} {r.cliente[:24]:<24} {r.mensagem[:55]}")

    rel = escrever_relatorio_lote(
        pasta_destino=CONTROLE_XLSX.parent,
        fluxo=FLUXO_DJEN,
        resultados=resultados,
        inicio=inicio_lote,
        instrucao_acao_manual=(
            "Buscar o CNJ manualmente em https://comunica.pje.jus.br e "
            "copiar nome + OAB dos advogados destinatarios do polo Ativo."
        ),
    )
    print(f"\n>>> Controle:               {CONTROLE_XLSX}")
    print(f">>> Planilha advogados:     {SAIDA_DJEN}")
    print(f">>> Relatorio do lote:      {rel}")

    return 0


def _eh_saida_djen(caminho: Path) -> bool:
    """True se a planilha tem cara de SAIDA do DJEN (tem coluna OAB).
    False se eh a planilha do Banco (CIV/CNJ, sem OAB) ou ilegivel."""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(str(caminho), read_only=True, data_only=True)
        ws = wb.active
        cab = next(ws.iter_rows(values_only=True), None)
        wb.close()
        if not cab:
            return False
        nomes = [str(c).strip().lower() for c in cab if c is not None]
        return "oab" in nomes
    except Exception:
        return False


def _djens_disponiveis():
    """Lista os advogados_djen_*.xlsx de saida/ (mais recentes primeiro),
    EXCLUINDO os advogados_djen_cna_*.xlsx (saida do CNA, nao entrada) -
    o glob 'advogados_djen_*' casaria eles tambem pelo prefixo comum."""
    import glob
    from core.config import ROOT
    cand = sorted(
        glob.glob(str(ROOT / "saida" / "advogados_djen_*.xlsx")),
        key=lambda p: Path(p).stat().st_mtime, reverse=True,
    )
    return [Path(c) for c in cand
            if not Path(c).name.startswith("~$")
            and "_cna_" not in Path(c).name]


def _djen_mais_recente():
    """Retorna o advogados_djen_*.xlsx mais recente em saida/, ou None."""
    djens = _djens_disponiveis()
    return djens[0] if djens else None


def _civs_da_coluna(caminho: Path) -> set:
    """Conjunto de CIVs (coluna 'CIV') de um xlsx. Vazio se nao achar."""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(str(caminho), read_only=True, data_only=True)
        ws = wb.active
        linhas = list(ws.iter_rows(values_only=True))
        wb.close()
    except Exception:
        return set()
    if not linhas:
        return set()
    cab = [str(c).strip().lower() if c is not None else "" for c in linhas[0]]
    try:
        idx = cab.index("civ")
    except ValueError:
        return set()
    out = set()
    for row in linhas[1:]:
        if idx < len(row) and row[idx]:
            out.add(str(row[idx]).strip().upper())
    return out


def _djen_que_cobre(civs_alvo: set):
    """Retorna o advogados_djen_*.xlsx mais recente cujas CIVs COBREM as
    civs_alvo (todas presentes), ou None se nenhum cobre. Usado pra evitar
    re-rodar o DJEN quando ja existe um resultado pra essas mesmas CIVs."""
    if not civs_alvo:
        return None
    for d in _djens_disponiveis():
        if civs_alvo.issubset(_civs_da_coluna(d)):
            return d
    return None


# ---------------------------------------------------------------------------
# Retomada da Tarefa 5 (CNA) entre execucoes
# ---------------------------------------------------------------------------
# Cada consulta ao CNA que FICA PENDENTE (captcha nao resolvido, rate-limit,
# timeout) nao tinha como ser distinguida, na planilha final, de um advogado
# que genuinamente nao cadastrou telefone/endereco - as duas situacoes
# ficavam com as colunas de contato em branco. Alem disso cada execucao
# reconsultava TODO MUNDO do zero, entao um captcha travando o lote fazia
# perder o progresso inteiro.
#
# A coluna "Status_CNA" (ver _status_cna) resolve a ambiguidade, e as
# funcoes abaixo leem TODAS as saidas advogados_djen_cna_*.xlsx ja geradas
# pra montar um cache de resultados DEFINITIVOS (OK_COM_CONTATO /
# OK_SEM_CONTATO / SEM_REGISTRO_CNA) por (OAB, UF) - PENDENTE nunca entra
# no cache, entao sempre e retentado na proxima rodada. Assim o lote
# CONVERGE em varias execucoes curtas em vez de exigir uma sessao inteira
# sem cair em captcha. --forcar ignora o cache e reconsulta tudo.

STATUS_CNA_DEFINITIVOS = {"OK_COM_CONTATO", "OK_SEM_CONTATO", "SEM_REGISTRO_CNA"}


def _status_cna(dado: dict) -> str:
    """Classifica o resultado de UMA consulta CNA - usado tanto pra
    gravar a coluna Status_CNA quanto pra decidir, numa execucao futura,
    se vale RETOMAR aquela linha.

    dado vazio ({})     -> NAO_CONSULTADO_CNA (pulado antes do CNA - DJEN
                            veio INCERTO/NAO ENCONTRADO/ERRO/sem OAB;
                            o problema e no DJEN, nao no CNA).
    ok=True             -> OK_COM_CONTATO ou OK_SEM_CONTATO (definitivo -
                            "sem contato" so significa que o advogado nao
                            cadastrou esses campos no CNA, nao que faltou
                            consultar).
    erro='sem resultado
    no CNA'             -> SEM_REGISTRO_CNA (a busca RODOU e nao achou a
                            inscricao - resultado negativo definitivo,
                            re-consultar so repetiria o mesmo).
    qualquer outro erro -> PENDENTE (captcha, rate-limit, timeout,
                            navegador travou etc. - nao sabemos a resposta
                            de verdade, entao vale retomar depois).
    """
    if not dado:
        return "NAO_CONSULTADO_CNA"
    if dado.get("ok"):
        tem = dado.get("telefone") or dado.get("email") or dado.get("endereco")
        return "OK_COM_CONTATO" if tem else "OK_SEM_CONTATO"
    if dado.get("erro") == "sem resultado no CNA":
        return "SEM_REGISTRO_CNA"
    return "PENDENTE"


def _cnas_disponiveis():
    """Lista os advogados_djen_cna_*.xlsx de saida/, mais antigos
    primeiro (ordem usada pro merge - o mais novo prevalece pra uma
    mesma chave OAB+UF)."""
    import glob
    from core.config import ROOT
    cand = sorted(
        glob.glob(str(ROOT / "saida" / "advogados_djen_cna_*.xlsx")),
        key=lambda p: Path(p).stat().st_mtime,
    )
    return [Path(c) for c in cand if not Path(c).name.startswith("~$")]


def _carregar_resultados_cna_previos() -> dict:
    """Le TODAS as saidas advogados_djen_cna_*.xlsx ja geradas e monta
    um cache (oab_so_digitos, uf) -> {status, telefone, email, endereco,
    subsecao, situacao} com os resultados DEFINITIVOS - PENDENTE nunca
    entra no cache (nem apaga um resultado bom que ja estava la de outra
    rodada), entao essas linhas sempre voltam a ser tentadas."""
    import openpyxl

    cache: dict = {}
    for caminho in _cnas_disponiveis():
        try:
            wb = openpyxl.load_workbook(str(caminho), read_only=True, data_only=True)
            ws = wb.active
            linhas = list(ws.iter_rows(values_only=True))
            wb.close()
        except Exception:
            continue
        if not linhas:
            continue

        cab = [str(c).strip() if c is not None else "" for c in linhas[0]]

        def _pos(nome):
            try:
                return [c.lower() for c in cab].index(nome.lower())
            except ValueError:
                return None

        p_oab, p_uf = _pos("OAB"), _pos("UF_OAB")
        p_status = _pos("Status_CNA")
        p_tel, p_email = _pos("Telefone_Profissional"), _pos("Email_Profissional")
        p_end, p_sub = _pos("Endereco_Profissional"), _pos("Subsecao_OAB")
        p_sit = _pos("Situacao_OAB")
        if p_oab is None or p_uf is None:
            continue

        def _cel(row, pos):
            if pos is None or pos >= len(row) or row[pos] is None:
                return ""
            return str(row[pos])

        for row in linhas[1:]:
            if p_oab >= len(row) or p_uf >= len(row):
                continue
            oab = re.sub(r"[^0-9]", "", _cel(row, p_oab))
            uf = _cel(row, p_uf).strip().upper()
            if not oab or not uf:
                continue
            tel, email = _cel(row, p_tel), _cel(row, p_email)
            end, sub, sit = _cel(row, p_end), _cel(row, p_sub), _cel(row, p_sit)

            if p_status is not None:
                status = _cel(row, p_status).strip().upper()
            else:
                # Arquivo de ANTES da coluna Status_CNA existir - infere.
                # "situacao" so vem preenchida quando a ficha foi aberta
                # com sucesso (ok=True); sem ela nao da pra confirmar se
                # o branco era um "sem contato" de verdade ou uma falha
                # antiga - trata como PENDENTE pra ficar do lado seguro.
                status = "OK_COM_CONTATO" if sit and (tel or email or end) \
                    else "OK_SEM_CONTATO" if sit else "PENDENTE"

            if status not in STATUS_CNA_DEFINITIVOS:
                continue  # PENDENTE - nunca entra no cache
            cache[(oab, uf)] = {
                "status": status, "telefone": tel, "email": email,
                "endereco": end, "subsecao": sub, "situacao": sit,
            }
    return cache


def main_cna(args) -> int:
    """Tarefa 5 standalone - enriquece advogados com Telefone/Endereco
    Profissional via CNA (cna.oab.org.br).

    Aceita DOIS tipos de entrada (detecta automaticamente e sinaliza):
      a) SAIDA do DJEN (advogados_djen_*.xlsx, com coluna OAB) -> usa direto.
      b) PLANILHA DO BANCO (CIV/CNJ, sem OAB) -> roda a Tarefa 4 (DJEN)
         antes pra obter os advogados, depois faz o CNA.
    Sem entrada informada -> usa o advogados_djen_*.xlsx mais recente.

    Consulta cada OAB+UF unico no CNA via Edge CDP e grava
    advogados_djen_cna_*.xlsx com as colunas de contato.

    Pre-condicao: Edge dedicado aberto (abrir-edge-bmg.bat). NAO precisa
    de login no e-Xyon - o CNA e publico.
    """
    import openpyxl
    from core.config import ROOT
    from fluxos.consultar_cna import (
        NOME_FLUXO as FLUXO_CNA,
        abrir_pagina_cna,
        consultar_por_inscricao,
    )
    from core.saida_djen_cna import registrar_cna

    print(f">>> {VERSAO} <<<")
    print(f">>> Tarefa 5 (standalone): {FLUXO_CNA}")
    print(f">>> Site:  https://cna.oab.org.br (navegador proprio, sem o Edge dedicado)")

    # 1) Resolve a entrada, detectando o tipo e SINALIZANDO qual foi usado.
    djen_path = None
    if args.djen_xlsx:
        djen_path = Path(args.djen_xlsx)
        print(f">>> Entrada: {djen_path.name}  [origem: --djen-xlsx]")
    elif args.planilha:
        p = Path(args.planilha)
        if not p.exists():
            print(f"\nERRO: arquivo informado nao encontrado: {p}")
            return 1
        if _eh_saida_djen(p):
            djen_path = p
            print(f">>> Entrada: {p.name}  [origem: saida DJEN informada]")
        else:
            print(f">>> Entrada: {p.name}  [origem: PLANILHA DO BANCO - sem coluna OAB]")
            # Verifica se o DJEN ja foi rodado pra essas CIVs (evita refazer).
            try:
                civs_planilha = {c.civ.strip().upper()
                                 for c in ler_planilha_casos(p) if c.civ}
            except Exception:
                civs_planilha = set()
            djen_previo = _djen_que_cobre(civs_planilha)
            if djen_previo:
                djen_path = djen_previo
                print(f">>> DJEN JA RODADO pra essas {len(civs_planilha)} CIV(s) - "
                      f"indo direto pro CNA.")
                print(f">>> Reaproveitando: {djen_path.name}")
            else:
                print(">>> Sem DJEN previo cobrindo essas CIVs. Rodando a "
                      "Tarefa 4 (DJEN) antes pra obter OAB...\n")
                try:
                    main_djen(args)
                except Exception as exc:
                    print(f">>> ERRO ao rodar DJEN: {type(exc).__name__}: {exc}")
                    return 1
                djen_path = _djen_mais_recente()
                print(f"\n>>> DJEN gerado. Seguindo o CNA com: "
                      f"{djen_path.name if djen_path else '(nao encontrado)'}")
    else:
        djen_path = _djen_mais_recente()
        if djen_path:
            print(f">>> Entrada: {djen_path.name}  [origem: advogados_djen mais recente em saida/]")

    if not djen_path or not djen_path.exists():
        print("\nERRO: nao encontrei planilha de advogados pra consultar.")
        print("Rode a Tarefa 4 (--so-djen) antes, ou informe a planilha do Banco.")
        return 1

    timestamp_arq = datetime.now().strftime("%Y-%m-%d_%H-%M")
    SAIDA_CNA = ROOT / "saida" / f"advogados_djen_cna_{timestamp_arq}.xlsx"
    print(f">>> Saida:          {SAIDA_CNA.name}\n")

    # 2) Le as linhas do DJEN
    wb_in = openpyxl.load_workbook(str(djen_path), read_only=True, data_only=True)
    ws_in = wb_in.active
    linhas_in = list(ws_in.iter_rows(values_only=True))
    wb_in.close()
    if not linhas_in or len(linhas_in) < 2:
        print("Planilha DJEN vazia. Nada a fazer.")
        return 0

    cabecalho = list(linhas_in[0])
    # Indices das colunas que precisamos (OAB e UF_OAB) - por nome
    def _idx(nome):
        try:
            return [str(c).strip().lower() for c in cabecalho].index(nome.lower())
        except ValueError:
            return None
    i_oab = _idx("OAB")
    i_uf = _idx("UF_OAB")
    i_nome = _idx("Advogado")
    i_status = _idx("Status")
    if i_oab is None or i_uf is None:
        print("ERRO: planilha DJEN sem colunas 'OAB'/'UF_OAB'. Formato inesperado.")
        return 1

    todas = [list(r) for r in linhas_in[1:] if any(c is not None for c in r)]
    # CNA so consulta advogados que SABEMOS ser do Autor: ENCONTRADO e
    # INFERIDO. INCERTO (pode incluir advogado do BMG), NAO ENCONTRADO e
    # ERRO sao pulados - nao gastar consulta nem contaminar a saida.
    # (feedback do operador em 17/06). Linhas puladas vao pra saida com as
    # colunas de contato em branco, pra nao sumirem do consolidado.
    STATUS_OK_CNA = {"ENCONTRADO", "INFERIDO"}
    if i_status is not None:
        dados = [r for r in todas
                 if str(r[i_status] or "").strip().upper() in STATUS_OK_CNA
                 and (i_oab < len(r) and str(r[i_oab] or "").strip())]
        pulados = [r for r in todas if r not in dados]
    else:
        dados = [r for r in todas if i_oab < len(r) and str(r[i_oab] or "").strip()]
        pulados = []
    if args.limite:
        dados = dados[: args.limite]
    print(f">>> {len(dados)} advogado(s) ENCONTRADO/INFERIDO pra consultar no CNA")
    if pulados:
        print(f">>> {len(pulados)} linha(s) puladas (INCERTO/NAO ENCONTRADO/ERRO/sem OAB)")

    def _chave(row):
        oab = str(row[i_oab]) if i_oab < len(row) and row[i_oab] is not None else ""
        uf = str(row[i_uf]) if i_uf < len(row) and row[i_uf] is not None else ""
        return (re.sub(r"[^0-9]", "", oab), uf.strip().upper())

    # Cabecalho fixo desde ja (nao depende de nenhuma consulta) - usado
    # pela gravacao incremental abaixo. Status_CNA torna cada linha
    # AUTO-EXPLICATIVA (ver _status_cna) e e o que permite a retomada
    # entre execucoes abaixo.
    novo_cabecalho = cabecalho + [
        "Telefone_Profissional", "Email_Profissional",
        "Endereco_Profissional", "Subsecao_OAB", "Situacao_OAB",
        "Status_CNA",
    ]

    def _escrever_linha(row, dado):
        """Grava UMA linha (CIV+advogado+contato) e salva na hora - NAO
        espera o lote inteiro. Chamar so quando o resultado da linha
        estiver definitivo (ver comentario em core/saida_djen_cna)."""
        valores = list(row) + [
            dado.get("telefone", ""), dado.get("email", ""),
            dado.get("endereco", ""), dado.get("subsecao", ""),
            dado.get("situacao", ""), _status_cna(dado),
        ]
        registrar_cna(SAIDA_CNA, novo_cabecalho, valores)

    # Linhas puladas (INCERTO/NAO ENCONTRADO/ERRO/DEFENSORIA/sem OAB) nao
    # dependem do CNA - grava JA, antes de abrir o navegador. Assim, mesmo
    # que o navegador nem abra, essas linhas ja estao salvas.
    for r in pulados:
        _escrever_linha(r, {})

    # Retomada entre execucoes: pula quem ja tem resultado DEFINITIVO
    # numa rodada anterior (ver _carregar_resultados_cna_previos). So
    # sobra pra consultar de novo: gente nova + quem ficou PENDENTE
    # (captcha/rate-limit/timeout) da ultima vez. --forcar desliga isso
    # e reconsulta todo mundo do zero.
    resultados_previos = {} if args.forcar else _carregar_resultados_cna_previos()
    if resultados_previos:
        ja_resolvidos = [r for r in dados if _chave(r) in resultados_previos]
    else:
        ja_resolvidos = []
    dados_a_consultar = [r for r in dados if _chave(r) not in resultados_previos]
    if ja_resolvidos:
        print(f">>> {len(ja_resolvidos)} advogado(s) ja resolvido(s) em rodada(s) "
              f"anterior(es) - reaproveitando (--forcar reconsulta tudo).")
        for r in ja_resolvidos:
            prev = resultados_previos[_chave(r)]
            _escrever_linha(r, {
                "ok": True, "telefone": prev["telefone"], "email": prev["email"],
                "endereco": prev["endereco"], "subsecao": prev["subsecao"],
                "situacao": prev["situacao"],
            })
    print(f">>> {len(dados_a_consultar)} advogado(s) a consultar nesta execucao\n")

    if not dados_a_consultar:
        if dados:
            print(">>> Nada pendente pro CNA - todo mundo ja tinha resultado "
                  "definitivo em rodada(s) anterior(es).")
        else:
            print(">>> Nenhum advogado ENCONTRADO/INFERIDO pra consultar no CNA.")
        print(f"\n>>> Planilha DJEN+CNA: {SAIDA_CNA}")
        return 0

    # 3) Navegador LIMPO e PERSISTENTE so pro CNA (.cna_profile), separado do
    # Edge dedicado cheio de automacao. Acumula reputacao no reCAPTCHA entre
    # rodadas -> cai menos no desafio de imagens. NAO precisa do Edge nem login.
    inicio_lote = datetime.now()
    perfil_cna = str(ROOT / ".cna_profile")
    cache = {}   # chave (oab,uf) -> dado; nao consulta o mesmo advogado 2x

    def _abrir_ctx(p, canal_preferido="msedge"):
        """Abre navegador CNA no canal preferido; fallback pro outro.
        Retorna (context, canal_usado). O `canal_usado` permite ao main
        alternar em caso de rate-limit (msedge -> chromium ou vice-versa).
        Cada canal usa um perfil proprio - a base persistente do Playwright
        nao pode ser compartilhada entre canais."""
        outros = [c for c in ("msedge", "chromium") if c != canal_preferido]
        for canal in [canal_preferido] + outros:
            kwargs = {"channel": "msedge"} if canal == "msedge" else {}
            perfil = perfil_cna if canal == "msedge" else perfil_cna + "_chromium"
            try:
                ctx = p.chromium.launch_persistent_context(
                    perfil, headless=False, **kwargs)
                aplicar_stealth_ao_contexto(ctx)
                return (ctx, canal)
            except Exception:
                continue
        return (None, None)

    def _processar(rows, page, etiqueta="", esperar_humano_no_captcha=False):
        """Consulta cada row, preenche o cache; retorna a lista de rows que
        ERRARAM (pra passada 2).

        Se o CNA exigir o desafio 'nao sou um robo', ABORTA o resto da
        passada: insistir so gera falha em serie e piora a reputacao no
        reCAPTCHA. O que ja foi consultado ja esta salvo (incremental).
        Na 1a passada isso e barato - o main so troca de navegador e
        refaz. Na 2a (esperar_humano_no_captcha=True) o operador ja foi
        chamado, entao abortar significa que ninguem atendeu."""
        from fluxos.consultar_cna import ERRO_CAPTCHA_CNA
        erros = []
        for idx, row in enumerate(rows, 1):
            oab = str(row[i_oab]) if i_oab < len(row) and row[i_oab] is not None else ""
            uf = str(row[i_uf]) if i_uf < len(row) and row[i_uf] is not None else ""
            nome = str(row[i_nome]) if (i_nome is not None and i_nome < len(row)
                                        and row[i_nome] is not None) else ""
            chave = _chave(row)
            if chave in cache and cache[chave].get("ok"):
                print(f"[{etiqueta}{idx}/{len(rows)}] {nome[:30]:30} OAB/{uf} {oab} (cache)")
                continue
            print(f"[{etiqueta}{idx}/{len(rows)}] {nome[:30]:30} OAB/{uf} {oab} ...", end=" ")
            try:
                dado = consultar_por_inscricao(
                    page, oab, uf, nome_hint=nome,
                    esperar_humano_no_captcha=esperar_humano_no_captcha,
                )
            except Exception as e:
                dado = {"telefone": "", "email": "", "endereco": "", "subsecao": "",
                        "situacao": "", "ok": False, "erro": f"{type(e).__name__}: {e}"}
            cache[chave] = dado
            if dado.get("ok"):
                tem = dado.get("telefone") or dado.get("email") or dado.get("endereco")
                print("OK" + (f" tel:{dado.get('telefone') or '-'} email:{dado.get('email') or '-'}"
                              if tem else " (sem contato)"))
            else:
                print(f"FALHA: {dado.get('erro','')}")
                erros.append(row)
                if dado.get("erro") == ERRO_CAPTCHA_CNA:
                    restantes = rows[idx:]
                    if esperar_humano_no_captcha:
                        print(f"\n>>> Desafio 'nao sou um robo' NAO foi "
                              f"resolvido a tempo. Parando o lote CNA "
                              f"({len(restantes)} advogado(s) nao "
                              f"consultado(s)).")
                        print(">>> O que ja foi consultado ESTA SALVO na planilha.")
                        print(">>> Rode a Tarefa 5 de novo com alguem por perto "
                              "pra resolver o desafio quando ele aparecer.\n")
                    else:
                        print(f"\n>>> Desafio 'nao sou um robo' apareceu. "
                              f"Interrompendo esta passada "
                              f"({len(restantes)} advogado(s) pendente(s)) "
                              f"pra tentar em outro navegador.\n")
                    erros.extend(restantes)
                    break
            # Pausa entre consultas. Subiu de 1-2s pra 3-6s (09/2026): ritmo
            # mais humano segura a nota do reCAPTCHA v3 e reduz a chance de
            # cair no desafio v2 no meio do lote.
            time.sleep(random.uniform(3.0, 6.0))
        return erros

    with sync_playwright() as p:
        context, canal_atual = _abrir_ctx(p)
        if context is None:
            print("\nERRO ao abrir navegador pro CNA.")
            print("Rode 'setup.ps1' (instala o navegador do Playwright).")
            return 1
        print(f">>> CNA canal: {canal_atual}")

        try:
            page = abrir_pagina_cna(context)
            erros = _processar(dados_a_consultar, page)   # PASSADA 1

            # Grava JA as que ficaram resolvidas na passada 1 (nao serao
            # retentadas) - nao espera o resto do lote.
            resolvidas_p1 = [r for r in dados_a_consultar if r not in erros]
            for r in resolvidas_p1:
                _escrever_linha(r, cache.get(_chave(r), {}))

            # PASSADA 2: reinicia o navegador e refaz SO os que erraram (1x).
            # Se algum erro foi 'LIMITE_CNA' (rate-limit modal do site),
            # TROCA de canal (msedge <-> chromium) - a nota do reCAPTCHA
            # v3 no navegador antigo ja esta baixa. Senao, reabre no mesmo
            # canal (basta pra captchas/instabilidade pontual).
            if erros:
                from fluxos.consultar_cna import ERRO_LIMITE_CNA, ERRO_CAPTCHA_CNA
                # Rate-limit e captcha nao-resolvido tem a MESMA cura: a nota
                # do reCAPTCHA v3 naquele navegador ja esta queimada, entao
                # vale reabrir no outro canal (perfil separado, nota propria).
                queimado = [r for r in erros
                            if cache.get(_chave(r), {}).get("erro")
                            in (ERRO_LIMITE_CNA, ERRO_CAPTCHA_CNA)]
                if queimado:
                    canal_novo = "chromium" if canal_atual == "msedge" else "msedge"
                    print(f"\n>>> Bloqueio do CNA (rate-limit/captcha) em "
                          f"{len(queimado)} caso(s). Reiniciando navegador e "
                          f"TROCANDO canal: {canal_atual} -> {canal_novo}\n")
                else:
                    canal_novo = canal_atual
                    print(f"\n>>> {len(erros)} advogado(s) com erro. Reiniciando o "
                          f"navegador e refazendo (passada 2, canal {canal_novo})...\n")
                try:
                    context.close()
                except Exception:
                    pass
                time.sleep(3)
                context, canal_atual = _abrir_ctx(p, canal_preferido=canal_novo)
                if context is not None:
                    print(f">>> CNA canal (passada 2): {canal_atual}")
                    page = abrir_pagina_cna(context)
                    # Ja trocamos de navegador. Se o desafio voltar mesmo
                    # assim, ai sim vale parar e chamar o operador.
                    _processar(erros, page, etiqueta="retry ",
                               esperar_humano_no_captcha=True)
                else:
                    print(">>> nao consegui reabrir o navegador pra passada 2.")

                # Passada 2 e a ULTIMA tentativa pra essas linhas - grava o
                # resultado final (sucesso OU falha) AGORA, incremental.
                for r in erros:
                    _escrever_linha(r, cache.get(_chave(r), {}))

            # Contadores pro resumo final - cada linha ja foi gravada no
            # disco (cache desta execucao OU resultados_previos de rodada
            # anterior); usa _status_cna pra bater exatamente com a coluna
            # Status_CNA da planilha.
            def _dado_final(row):
                chave = _chave(row)
                if chave in cache:
                    return cache[chave]
                if chave in resultados_previos:
                    prev = resultados_previos[chave]
                    return {"ok": True, "telefone": prev["telefone"],
                            "email": prev["email"], "endereco": prev["endereco"],
                            "subsecao": prev["subsecao"], "situacao": prev["situacao"]}
                return {}

            contas = {}
            for row in dados:
                st = _status_cna(_dado_final(row))
                contas[st] = contas.get(st, 0) + 1
            pendentes = contas.get("PENDENTE", 0)

            print(f"\n{'=' * 72}")
            print(f"  RESUMO — {FLUXO_CNA}")
            print(f"{'=' * 72}")
            print(f"  Linhas (ENCONTRADO/INFERIDO): {len(dados)}")
            print(f"  [OK com contato]:    {contas.get('OK_COM_CONTATO', 0)}")
            print(f"  [OK sem contato]:    {contas.get('OK_SEM_CONTATO', 0)}")
            print(f"  [sem registro CNA]:  {contas.get('SEM_REGISTRO_CNA', 0)}")
            print(f"  [PENDENTE - retoma]: {pendentes}")
            print(f"{'=' * 72}")
            if pendentes:
                print(f">>> {pendentes} advogado(s) ainda PENDENTE(s) (captcha/"
                      f"rate-limit/timeout nao resolvidos). Rode a Tarefa 5 de "
                      f"novo - so eles serao reconsultados.")
            print(f"\n>>> Planilha DJEN+CNA: {SAIDA_CNA}")

        finally:
            try:
                context.close()
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
