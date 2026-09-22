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
                 "Capturado_em"])

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


def registrar_financeiro(caminho_xlsx: Path, linha: LinhaFinanceiro) -> None:
    """Adiciona uma linha (cria o arquivo se nao existir)."""
    caminho_xlsx = Path(caminho_xlsx)
    caminho_xlsx.parent.mkdir(parents=True, exist_ok=True)
    if not linha.capturado_em:
        linha.capturado_em = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    wb, ws = _garantir_workbook(caminho_xlsx)
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
    ws.append(valores)
    n_col = len(_CABECALHO)
    # Colunas na ordem (a partir do fim): Capturado_em, Observacao_Negociador,
    # Nao_Exigivel, Motivo_Revisao, Revisar.
    if revisar:
        ws.cell(row=ws.max_row, column=n_col - 4).fill = _FILL_REVISAR
    if nao_exigivel:
        ws.cell(row=ws.max_row, column=n_col - 2).fill = _FILL_NAO_EXIGIVEL
    salvar_workbook_com_retry(wb, caminho_xlsx)
    wb.close()


def ja_capturado(caminho_xlsx: Path, civ: str) -> bool:
    caminho_xlsx = Path(caminho_xlsx)
    if not caminho_xlsx.exists():
        return False
    civ_norm = (civ or "").strip().upper()
    if not civ_norm:
        return False
    wb = openpyxl.load_workbook(str(caminho_xlsx), read_only=True, data_only=True)
    try:
        ws = wb.active
        for row in ws.iter_rows(min_row=2, max_col=1, values_only=True):
            cel = row[0]
            if cel and str(cel).strip().upper() == civ_norm:
                return True
    finally:
        wb.close()
    return False


def ja_capturado_em_pasta(pasta_saida: Path, civ: str) -> bool:
    """Idempotencia cross-arquivos: True se a CIV ja foi capturada em
    QUALQUER financeiro_*.xlsx da pasta."""
    pasta_saida = Path(pasta_saida)
    if not pasta_saida.exists():
        return False
    civ_norm = (civ or "").strip().upper()
    if not civ_norm:
        return False
    for arq in sorted(pasta_saida.glob("financeiro_*.xlsx")):
        if arq.name.startswith("~$"):
            continue
        try:
            if ja_capturado(arq, civ):
                return True
        except Exception:
            continue
    return False
