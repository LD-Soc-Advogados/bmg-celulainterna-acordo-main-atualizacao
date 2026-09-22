"""
Writer INCREMENTAL da planilha de saida da Tarefa 5 (contato via CNA).

Ate 2026-09 a Tarefa 5 acumulava TODAS as consultas em memoria (dict
`cache`) e so gravava o .xlsx UMA vez, no final do lote inteiro - depois
das 2 passadas (normal + retry). O bloco que fazia a consulta nem tinha
`except:` ao redor, so `finally:` fechando o navegador - qualquer
interrupcao (navegador travou, rede caiu, PC dormiu, fechou a janela,
Ctrl+C) propagava pra fora ANTES da gravacao, e o lote INTEIRO se
perdia, mesmo as consultas que ja tinham dado certo.

As Tarefas 1 (Financeiro), 2 (Downloads) e 4 (DJEN) ja gravavam
incrementalmente (por CIV / por documento). Este modulo faz o mesmo
pra Tarefa 5: grava UMA linha (CIV + advogado + contato) assim que o
resultado dela estiver definitivo, sem esperar o lote inteiro.
"""
from pathlib import Path

import openpyxl
from openpyxl.styles import Font

from .relatorio import salvar_workbook_com_retry, sanitize_xlsx, carregar_workbook_seguro


def _garantir_workbook(caminho: Path, cabecalho: list):
    wb = carregar_workbook_seguro(caminho)
    if wb is not None:
        return wb, wb.active

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Advogados DJEN+CNA"
    ws.append(cabecalho)
    for col_idx in range(1, len(cabecalho) + 1):
        ws.cell(row=1, column=col_idx).font = Font(bold=True)
    return wb, ws


def registrar_cna(caminho_xlsx: Path, cabecalho: list, valores: list) -> None:
    """Grava UMA linha e salva na hora (nao acumula em memoria).

    cabecalho: usado so na criacao do arquivo (1a linha, negrito). Se o
    arquivo ja existe, o cabecalho gravado nele prevalece - assume-se
    consistente dentro da mesma execucao.
    valores:   lista de celulas na MESMA ordem do cabecalho.

    Chamar assim que o resultado da linha estiver definitivo (sucesso,
    ou falha apos a ultima tentativa) - e o que garante que uma
    interrupcao no meio do lote so perde a linha em andamento, nao o
    lote inteiro.
    """
    caminho_xlsx = Path(caminho_xlsx)
    caminho_xlsx.parent.mkdir(parents=True, exist_ok=True)
    wb, ws = _garantir_workbook(caminho_xlsx, cabecalho)
    ws.append([sanitize_xlsx(v) if isinstance(v, str) else v for v in valores])
    salvar_workbook_com_retry(wb, caminho_xlsx)
    wb.close()
