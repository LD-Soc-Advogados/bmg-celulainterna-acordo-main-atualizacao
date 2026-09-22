"""
Writer da planilha saida/contatos_advogados.xlsx.

Schema (uma linha por CIV):
  CIV | Cliente | CNJ | Adv1 | OAB1 | UF1 | Email1 | Tel1 | Cel1 |
  Adv2 | ... (ate Adv3) | Email_Parte_Autora |
  Fonte | Confianca | OCR_Pendente | Observacao
"""
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl.styles import Font

from .relatorio import salvar_workbook_com_retry, sanitize_xlsx, carregar_workbook_seguro


MAX_ADV_NA_PLANILHA = 3  # exporta os 3 primeiros; restantes vao pra Observacao


def _cabecalho() -> list:
    h = ["CIV", "Cliente", "CNJ"]
    for i in range(1, MAX_ADV_NA_PLANILHA + 1):
        h += [f"Advogado_{i}", f"OAB_{i}", f"UF_{i}",
              f"Email_{i}", f"Telefone_{i}", f"Celular_{i}"]
    h += ["Email_Parte_Autora", "Fonte", "Confianca",
          "OCR_Pendente", "Observacao", "Capturado_em"]
    return h


@dataclass
class LinhaContato:
    civ: str = ""
    cliente: str = ""
    cnj: str = ""
    advogados: list = field(default_factory=list)  # list[Advogado] do extrator
    email_parte_autora: str = ""
    fonte: str = ""           # "peticao_inicial.pdf,procuracao.pdf"
    confianca: str = ""       # ALTA / MEDIA / BAIXA
    ocr_pendente: str = ""    # nomes dos PDFs que precisam OCR
    observacao: str = ""
    capturado_em: str = ""


def _garantir_workbook(caminho: Path):
    wb = carregar_workbook_seguro(caminho)
    if wb is not None:
        return wb, wb.active

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Contatos"
    cab = _cabecalho()
    ws.append(cab)
    for col_idx in range(1, len(cab) + 1):
        ws.cell(row=1, column=col_idx).font = Font(bold=True)
    return wb, ws


def _linha_row(linha: LinhaContato) -> list:
    s = sanitize_xlsx
    row = [s(linha.civ), s(linha.cliente), s(linha.cnj)]
    for i in range(MAX_ADV_NA_PLANILHA):
        if i < len(linha.advogados):
            a = linha.advogados[i]
            row += [s(a.nome), s(a.oab), s(a.uf), s(a.email), s(a.telefone), s(a.celular)]
        else:
            row += ["", "", "", "", "", ""]
    row += [
        s(linha.email_parte_autora),
        s(linha.fonte),
        s(linha.confianca),
        s(linha.ocr_pendente),
        s(linha.observacao),
        s(linha.capturado_em),
    ]
    return row


def registrar_contato(caminho_xlsx: Path, linha: LinhaContato) -> None:
    caminho_xlsx = Path(caminho_xlsx)
    caminho_xlsx.parent.mkdir(parents=True, exist_ok=True)
    if not linha.capturado_em:
        linha.capturado_em = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    wb, ws = _garantir_workbook(caminho_xlsx)
    ws.append(_linha_row(linha))
    salvar_workbook_com_retry(wb, caminho_xlsx)
    wb.close()


def ja_extraido(caminho_xlsx: Path, civ: str) -> bool:
    """True se a CIV ja tem linha (idempotencia em re-run)."""
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
