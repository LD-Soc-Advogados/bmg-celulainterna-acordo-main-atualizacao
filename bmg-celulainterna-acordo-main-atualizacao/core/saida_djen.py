"""
Writer da planilha de saida da Tarefa 4 (advogados via DJEN).

Schema (UMA LINHA POR ADVOGADO; CIVs sem advogado tambem entram, com
uma linha de status pra nao sumirem do consolidado):

  CIV | Cliente | CNJ | Estado | Status | Advogado | OAB | UF_OAB |
  Ultima_Comunicacao | Tribunal | N_Comunicacoes | Observacao | Capturado_em

Status:
  ENCONTRADO     - advogado(s) do polo Autor localizado(s) em comunicacao
                   exclusiva do Autor (alta confianca)
  INFERIDO       - processo so tinha comunicacao com Banco+Autor; o
                   advogado foi mapeado por POSICAO (parte do Banco
                   identificada pelo nome). CONFERIR antes de usar.
  INCERTO        - ha advogados mas nao deu pra separar Autor/Banco (nomes
                   das partes abreviados). Lista todos - PODE INCLUIR
                   advogados do BMG. (A Tarefa 5/CNA ignora esses.)
  NAO ENCONTRADO - sem advogado nas comunicacoes do processo
  ERRO           - falha tecnica (API fora, CNJ invalido, etc.)

O advogado mais recente (= atual da causa) vem primeiro dentro de cada
CIV. Cada execucao APPENDA ao arquivo da rodada (criado com timestamp
pelo main_djen). Manter TODAS as CIVs na saida e proposital: o operador
usa essa planilha pra consolidar o fluxo de acordo, e precisa enxergar
tambem o que faltou (com o motivo) pra resolver manualmente.
"""
from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl.styles import Font

from .relatorio import salvar_workbook_com_retry, sanitize_xlsx, carregar_workbook_seguro


_CABECALHO = [
    "CIV", "Cliente", "CNJ", "Estado", "Status",
    "Advogado", "OAB", "UF_OAB", "Na_Lista_Agressores",
    "Ultima_Comunicacao", "Tribunal", "N_Comunicacoes",
    "Observacao", "Capturado_em",
]

STATUS_ENCONTRADO = "ENCONTRADO"
STATUS_INFERIDO = "INFERIDO"
STATUS_INCERTO = "INCERTO"
STATUS_DEFENSORIA = "DEFENSORIA"
STATUS_NAO_ENCONTRADO = "NAO ENCONTRADO"
STATUS_ERRO_DJEN = "ERRO"


def _garantir_workbook(caminho: Path):
    wb = carregar_workbook_seguro(caminho)
    if wb is not None:
        return wb, wb.active

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Advogados DJEN"
    ws.append(_CABECALHO)
    for col_idx in range(1, len(_CABECALHO) + 1):
        ws.cell(row=1, column=col_idx).font = Font(bold=True)
    return wb, ws


def registrar_djen(caminho_xlsx: Path, civ: str, cliente: str, cnj: str,
                   estado: str, advogados: list, status: str,
                   motivo: str = "", capturado_em: str = "") -> None:
    """Grava o resultado DJEN de UMA CIV.

    advogados: list[AdvogadoDJEN] (pode ser vazia).
    status:    ENCONTRADO / NAO ENCONTRADO / ERRO.
    motivo:    texto explicativo quando nao houve advogado (vai em Observacao).

    Se 'advogados' tem itens -> 1 linha por advogado (Status=ENCONTRADO).
    Se vazia -> 1 linha so, com Status + Observacao e campos de advogado
                em branco (a CIV NAO some do consolidado).
    """
    caminho_xlsx = Path(caminho_xlsx)
    caminho_xlsx.parent.mkdir(parents=True, exist_ok=True)
    if not capturado_em:
        capturado_em = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Flag "Na_Lista_Agressores" por advogado (nao exclui, so sinaliza).
    try:
        from fluxos.consultar_djen import eh_agressor
    except Exception:
        eh_agressor = lambda _n, _o: False

    wb, ws = _garantir_workbook(caminho_xlsx)

    if advogados:
        for adv in advogados:
            agressor = "SIM" if eh_agressor(adv.nome, adv.oab) else "NAO"
            ws.append([
                sanitize_xlsx(civ),
                sanitize_xlsx(cliente),
                sanitize_xlsx(cnj),
                sanitize_xlsx(estado),
                sanitize_xlsx(status),
                sanitize_xlsx(adv.nome),
                sanitize_xlsx(adv.oab),
                sanitize_xlsx(adv.uf_oab),
                agressor,
                sanitize_xlsx(adv.ultima_comunicacao),
                sanitize_xlsx(adv.tribunal),
                adv.n_comunicacoes,
                sanitize_xlsx(motivo),
                sanitize_xlsx(capturado_em),
            ])
    else:
        ws.append([
            sanitize_xlsx(civ),
            sanitize_xlsx(cliente),
            sanitize_xlsx(cnj),
            sanitize_xlsx(estado),
            sanitize_xlsx(status),
            "", "", "", "", "", "", "",
            sanitize_xlsx(motivo),
            sanitize_xlsx(capturado_em),
        ])

    salvar_workbook_com_retry(wb, caminho_xlsx)
    wb.close()


def registrar_linha_djen(caminho_xlsx: Path, linha) -> None:
    """Compat: grava a partir de uma LinhaDJEN (so quando ha advogados).
    Mantido pra nao quebrar chamadas antigas; o fluxo novo usa
    registrar_djen() diretamente pra incluir as CIVs sem advogado."""
    if linha is None or not linha.advogados:
        return
    registrar_djen(
        caminho_xlsx, linha.civ, linha.cliente, linha.cnj, linha.estado,
        linha.advogados, STATUS_ENCONTRADO, "", linha.capturado_em,
    )
