"""
Writer da planilha de saida da Tarefa 1 (dados da aba Financeiro).

Substitui a antiga saida de mensagens: o Banco parou de mandar a mensagem
de PEDIDO DE PAGAMENTO, entao a Tarefa 1 agora extrai TODOS os campos da
aba Financeiro (ver core/aba_financeiro.CAMPOS).

Schema:
  CIV | Cliente | CNJ | Estado | <um por campo do Financeiro> | Estagio |
  Revisar | Motivo_Revisao | Nao_Exigivel | Observacao_Negociador |
  Capturado_em

Estagio = valor BRUTO da coluna Estagio do painel (Finalizado, Aprovação
Alçada, ...) - ver core/aba_financeiro.CHAVE_ESTAGIO.

Revisar = "SIM" quando o robo teve que pular lancamento(s) de custas ou
honorarios pra chegar no pagamento de condenacao (ver core/aba_financeiro).

Cada execucao gera um arquivo datado 'financeiro_YYYY-MM-DD_HH-MM.xlsx' e
APPENDA linhas. Idempotencia cross-arquivos via ja_capturado_em_pasta
(varre financeiro_*.xlsx).
"""
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl.styles import Font, PatternFill

from .aba_financeiro import (
    CAMPOS,
    CHAVE_REVISAR, CHAVE_MOTIVO_REVISAO,
    CHAVE_ESTAGIO,
    CHAVE_NAO_EXIGIVEL, CHAVE_MOTIVO_NAO_EXIGIVEL,
)
from .relatorio import salvar_workbook_com_retry, sanitize_xlsx, carregar_workbook_seguro


# Cabecalho: fixos + um por campo do Financeiro (usa o rotulo do painel)
# + Estagio (valor bruto da coluna, sem rotulo no painel) + colunas de
# sinalizacao pro operador + timestamp.
_ROTULOS_CAMPO = [rotulo for _chave, rotulo in CAMPOS]
_CHAVES_CAMPO = [chave for chave, _rotulo in CAMPOS]
_CABECALHO = (["CIV", "Cliente", "CNJ", "Estado"] + _ROTULOS_CAMPO
              + ["Estagio",
                 "Revisar", "Motivo_Revisao",
                 "Nao_Exigivel", "Observacao_Negociador",
                 "Capturado_em", "Status", "Motivo", "Arquivo_Origem"])

# Destaques na saida:
#   - Revisar = amarelo suave (lancamento nao-condenacao usado como fallback)
#   - Nao_Exigivel = vermelho forte (pagamento finalizado, nao cabe acordo)
_FILL_REVISAR = PatternFill("solid", fgColor="FFF2CC")
_FILL_NAO_EXIGIVEL = PatternFill("solid", fgColor="F8CBAD")


@dataclass
class LinhaFinanceiro:
    civ: str
    cliente: str
    cnj: str
    estado: str
    # dict {chave: valor} vindo de aba_financeiro.extrair_financeiro().
    campos: dict = field(default_factory=dict)
    capturado_em: str = ""
    status: str = "OK"
    motivo: str = ""
    arquivo_origem: str = ""


def _garantir_workbook(caminho: Path):
    wb = carregar_workbook_seguro(caminho)
    if wb is not None:
        return wb, wb.active

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Financeiro"
    ws.append(_CABECALHO)
    for col_idx in range(1, len(_CABECALHO) + 1):
        ws.cell(row=1, column=col_idx).font = Font(bold=True)
    return wb, ws


def _garantir_colunas(ws):
    existentes = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)] if ws.max_row else []
    if not existentes or not existentes[0]:
        ws.append(_CABECALHO)
        for c in range(1, len(_CABECALHO) + 1):
            ws.cell(1, c).font = Font(bold=True)
        return
    for coluna in _CABECALHO:
        if coluna not in existentes:
            ws.cell(1, ws.max_column + 1, coluna).font = Font(bold=True)
            existentes.append(coluna)


def registrar_financeiro(caminho_xlsx: Path, linha: LinhaFinanceiro) -> None:
    """Adiciona uma linha (cria o arquivo se nao existir)."""
    caminho_xlsx = Path(caminho_xlsx)
    caminho_xlsx.parent.mkdir(parents=True, exist_ok=True)
    if not linha.capturado_em:
        linha.capturado_em = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    wb, ws = _garantir_workbook(caminho_xlsx)
    _garantir_colunas(ws)
    valores = [
        sanitize_xlsx(linha.civ),
        sanitize_xlsx(linha.cliente),
        sanitize_xlsx(linha.cnj),
        sanitize_xlsx(linha.estado),
    ]
    for chave in _CHAVES_CAMPO:
        valores.append(sanitize_xlsx(str(linha.campos.get(chave, "") or "")))
    revisar = str(linha.campos.get(CHAVE_REVISAR, "") or "")
    nao_exigivel = str(linha.campos.get(CHAVE_NAO_EXIGIVEL, "") or "")
    valores.append(sanitize_xlsx(str(linha.campos.get(CHAVE_ESTAGIO, "") or "")))
    valores.append(sanitize_xlsx(revisar))
    valores.append(sanitize_xlsx(str(linha.campos.get(CHAVE_MOTIVO_REVISAO, "") or "")))
    valores.append(sanitize_xlsx(nao_exigivel))
    valores.append(sanitize_xlsx(str(linha.campos.get(CHAVE_MOTIVO_NAO_EXIGIVEL, "") or "")))
    valores.append(sanitize_xlsx(linha.capturado_em))
    valores.extend([sanitize_xlsx(linha.status), sanitize_xlsx(linha.motivo), sanitize_xlsx(linha.arquivo_origem)])
    indices = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
    row = ws.max_row + 1
    for coluna, valor in zip(_CABECALHO, valores):
        ws.cell(row, indices[coluna], valor)
    # Colunas na ordem (a partir do fim): Capturado_em, Observacao_Negociador,
    # Nao_Exigivel, Motivo_Revisao, Revisar.
    if revisar:
        ws.cell(row=row, column=indices["Revisar"]).fill = _FILL_REVISAR
    if nao_exigivel:
        ws.cell(row=row, column=indices["Nao_Exigivel"]).fill = _FILL_NAO_EXIGIVEL
    salvar_workbook_com_retry(wb, caminho_xlsx)
    wb.close()


def obter_captura_valida(caminho_xlsx: Path, civ: str):
    """Localiza somente uma captura anterior com dados financeiros validos."""
    caminho_xlsx = Path(caminho_xlsx)
    if not caminho_xlsx.exists():
        return None
    civ_norm = (civ or "").strip().upper()
    if not civ_norm:
        return None
    wb = openpyxl.load_workbook(str(caminho_xlsx), read_only=True, data_only=True)
    try:
        ws = wb.active
        headers = {str(c.value).strip(): c.column for c in ws[1] if c.value}
        idx_civ = headers.get("CIV", 1)
        for row in ws.iter_rows(min_row=2, values_only=True):
            cel = row[idx_civ - 1] if len(row) >= idx_civ else None
            if not cel or str(cel).strip().upper() != civ_norm:
                continue
            status = str(row[headers["Status"] - 1] or "").upper() if "Status" in headers else "OK"
            campos = {chave: (row[headers[rotulo] - 1] or "") for chave, rotulo in CAMPOS if rotulo in headers}
            if status in ("", "OK", "JA_CAPTURADO") and campos.get("cod_lancto") and (campos.get("tipo_lancto") or campos.get("data_pagamento") or campos.get("valor")):
                return campos, caminho_xlsx.name
    finally:
        wb.close()
    return None

def ja_capturado(caminho_xlsx: Path, civ: str) -> bool:
    return obter_captura_valida(caminho_xlsx, civ) is not None

def ja_capturado_em_pasta(pasta_saida: Path, civ: str):
    """Retorna dados + arquivo apenas se existir captura valida."""
    pasta_saida = Path(pasta_saida)
    if not pasta_saida.exists():
        return None
    for arq in sorted(pasta_saida.glob("financeiro_*.xlsx")):
        if arq.name.startswith("~$"):
            continue
        try:
            captura = obter_captura_valida(arq, civ)
            if captura:
                return captura
        except Exception:
            continue
    return None
