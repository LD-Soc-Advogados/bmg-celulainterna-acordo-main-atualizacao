"""
Manifesto da rodada: lista CIV | Cliente (parte) | CNJ | Pasta de download.

Usado pra rodar o Claude Cowork so nas pastas da ULTIMA rodada (extrair
contatos dos PDFs baixados). Gerado automaticamente no fim de cada lote
e sob demanda pela GUI.
"""
from pathlib import Path

import openpyxl
from openpyxl.styles import Font

from .relatorio import salvar_workbook_com_retry, sanitize_xlsx


_CABECALHO = ["CIV", "Cliente", "CNJ", "Pasta_Download", "Pasta_Existe", "N_PDFs"]


def gerar_manifesto(casos, downloads_dir, destino: Path) -> Path:
    """Escreve o manifesto (uma linha por CIV) em `destino` (.xlsx).

    casos: lista de objetos com .civ, .cliente, .cnj (CasoAutorizado).
    downloads_dir: raiz das pastas de cliente.
    Retorna o Path escrito.
    """
    from fluxos.baixar_documentos import slug_pasta_processo  # evita import circular

    downloads_dir = Path(downloads_dir)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Manifesto"
    ws.append(_CABECALHO)
    for i in range(len(_CABECALHO)):
        ws.cell(row=1, column=i + 1).font = Font(bold=True)

    for caso in casos:
        cliente = getattr(caso, "cliente", "") or ""
        cnj = getattr(caso, "cnj", "") or ""
        civ = getattr(caso, "civ", "") or ""
        pasta = downloads_dir / slug_pasta_processo(cliente, cnj, civ)
        existe = pasta.exists()
        n_pdfs = len(list(pasta.glob("*.pdf"))) if existe else 0
        ws.append([
            sanitize_xlsx(getattr(caso, "civ", "")),
            sanitize_xlsx(cliente),
            sanitize_xlsx(getattr(caso, "cnj", "")),
            sanitize_xlsx(str(pasta)),
            "SIM" if existe else "NAO",
            n_pdfs,
        ])

    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    salvar_workbook_com_retry(wb, destino)
    wb.close()
    return destino
