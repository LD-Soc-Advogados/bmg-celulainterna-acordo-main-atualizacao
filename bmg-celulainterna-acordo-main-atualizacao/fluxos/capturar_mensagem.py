"""
Tarefa 1 - Extrair os dados da aba FINANCEIRO do e-Xyon.

Desde 06/2026 o Banco parou de mandar a mensagem de "PEDIDO DE PAGAMENTO"
(as solicitacoes de pagamento nao geram mais mensagem interna). Toda a
informacao fica SO na aba Financeiro. Por isso a Tarefa 1 deixou de
capturar mensagem e passou a extrair TODOS os campos do lancamento de
pagamento mais recente do painel "Solicitacoes" (ver core/aba_financeiro).

Saida: uma linha por CIV na planilha financeiro_*.xlsx (uma coluna por
campo) e uma linha no controle.xlsx (status OK/ERRO/JA_FECHADO).

O modulo mantem o nome do arquivo (capturar_mensagem.py) e os simbolos
processar_captura / registrar_se_ok / NOME_FLUXO por compatibilidade com
o main.py.
"""
from datetime import datetime

from playwright.sync_api import Page

from core.aba_financeiro import (
    capturar_financeiro,
    CHAVE_ESTAGIO,
    CHAVE_REVISAR, CHAVE_MOTIVO_REVISAO,
    CHAVE_NAO_EXIGIVEL, CHAVE_MOTIVO_NAO_EXIGIVEL,
)
from core.processo import extrair_estado_do_cnj
from core.relatorio import (
    ResultadoLinha,
    STATUS_OK,
    STATUS_ERRO,
    STATUS_JA_FECHADO,
)
from core.saida_financeiro import (
    LinhaFinanceiro, registrar_financeiro, ja_capturado_em_pasta,
)
from core.config import SAIDA_DIR, get_saida_financeiro_atual


NOME_FLUXO = "Extrair Financeiro"


def processar_captura(
    page: Page,
    civ: str,
    cliente: str,
    cnj: str,
    forcar: bool = False,
) -> tuple:
    """Extrai a aba Financeiro de UMA CIV. A pagina ja deve estar no
    processo aberto.

    Retorna (ResultadoLinha, LinhaFinanceiro ou None). A LinhaFinanceiro
    so eh retornada (e gravada) se extraiu algum lancamento.

    forcar: se True, pula a idempotencia (ja_capturado_em_pasta).
    """
    estado = extrair_estado_do_cnj(cnj) if cnj else ""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _resultado(status: str, msg: str, linha=None):
        rl = ResultadoLinha(
            civ=civ, cliente=cliente, cnj=cnj, estado=estado,
            fluxo=NOME_FLUXO, status=status, mensagem=msg,
            timestamp=timestamp,
        )
        if linha is None:
            etapa = "JA_CAPTURADO" if status == STATUS_JA_FECHADO else ("FALHA_LEITURA" if "leitura" in msg.lower() or "campo" in msg.lower() or "formato" in msg.lower() else "ABA_SEM_DADOS")
            linha = LinhaFinanceiro(civ=civ, cliente=cliente, cnj=cnj, estado=estado,
                                    campos={}, capturado_em=timestamp, status=etapa, motivo=msg)
        return (rl, linha)

    # Reaproveita somente captura anterior com campos financeiros validos.
    if not forcar:
        anterior = ja_capturado_em_pasta(SAIDA_DIR, civ)
        if anterior:
            campos_antigos, arquivo_origem = anterior
            linha = LinhaFinanceiro(civ=civ, cliente=cliente, cnj=cnj, estado=estado,
                                    campos=campos_antigos, capturado_em=timestamp,
                                    status="JA_CAPTURADO", motivo="Dados financeiros reaproveitados de captura valida.",
                                    arquivo_origem=arquivo_origem)
            return _resultado(STATUS_JA_FECHADO, f"Captura valida reaproveitada de {arquivo_origem}", linha)

    # Extrai TODOS os campos do lancamento de pagamento mais recente.
    campos = capturar_financeiro(page, civ)
    if not campos or campos.get("_status"):
        diagnostico = campos or {}
        codigo = diagnostico.get("_status", "ABA_SEM_DADOS")
        motivo = diagnostico.get("_motivo", "Aba Financeiro sem lancamento de pagamento.")
        linha = LinhaFinanceiro(civ=civ, cliente=cliente, cnj=cnj, estado=estado,
                                capturado_em=timestamp, status=codigo, motivo=motivo)
        return _resultado(STATUS_ERRO, f"{codigo}: {motivo}", linha)

    linha = LinhaFinanceiro(
        civ=civ, cliente=cliente, cnj=cnj, estado=estado,
        campos=campos, capturado_em=timestamp, status="OK",
    )
    dp = campos.get("data_pagamento") or "-"
    val = campos.get("valor") or "-"
    saldo = campos.get("saldo_atualizado") or "-"
    msg = f"Financeiro: data {dp} | valor {val} | saldo {saldo}"
    if campos.get(CHAVE_ESTAGIO):
        msg += f" | Estagio: {campos.get(CHAVE_ESTAGIO)}"
    if campos.get(CHAVE_REVISAR):
        msg += f" | REVISAR: {campos.get(CHAVE_MOTIVO_REVISAO) or 'conferir lancamento'}"
    if campos.get(CHAVE_NAO_EXIGIVEL):
        msg += (f" | NAO EXIGIVEL: "
                f"{campos.get(CHAVE_MOTIVO_NAO_EXIGIVEL) or 'solicitacao finalizada'}")
    return _resultado(STATUS_OK, msg, linha)


def registrar_se_ok(linha_financeiro, saida_path=None):
    """Grava a LinhaFinanceiro na planilha da rodada.

    saida_path: path da planilha da RODADA ATUAL (com timestamp). Se None,
    cria um novo com get_saida_financeiro_atual().
    """
    destino = saida_path if saida_path is not None else get_saida_financeiro_atual()
    if linha_financeiro is None:
        return
    registrar_financeiro(destino, linha_financeiro)
