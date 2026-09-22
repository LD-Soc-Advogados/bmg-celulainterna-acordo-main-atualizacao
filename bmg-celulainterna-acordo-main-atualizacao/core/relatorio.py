"""
Writer do controle.xlsx (acumulado entre execucoes) e do
_LOTE_RELATORIO_{data}.txt (sumario do lote).

Schema do controle.xlsx:
  CIV | Cliente | CNJ | Estado | Fluxo | Status | Mensagem | Timestamp
"""
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl.styles import Font


# Caracteres de controle proibidos no XML/XLSX (openpyxl bloqueia com
# IllegalCharacterError). Vimos U+0001-U+0008, U+000B-U+000C, U+000E-U+001F
# e tambem U+263B (smiley face de controle) aparecendo em mensagens do
# e-Xyon - aparente corrupcao de encoding. Sanitiza tudo antes de gravar.
_ILLEGAL_XLSX = re.compile(
    r"[\x00-\x08\x0B-\x0C\x0E-\x1F"
    r"☺-☿"  # smileys (apareceu '☻' = U+263B numa mensagem real)
    r"]"
)


def sanitize_xlsx(valor):
    """Remove caracteres ilegais pro XLSX. Aceita qualquer tipo, retorna
    o original sem mudanca se nao for string."""
    if not isinstance(valor, str):
        return valor
    if not valor:
        return valor
    cleaned = _ILLEGAL_XLSX.sub("", valor)
    return cleaned


def salvar_workbook_com_retry(
    wb,
    caminho_xlsx: Path,
    max_tentativas: int = 3,
    intervalo_s: float = 2.0,
) -> Path:
    """Salva o workbook tentando max_tentativas vezes (com pausa entre).

    Se todas falharem, salva num arquivo de BACKUP no mesmo diretorio
    (`<stem>_backup_<timestamp>.xlsx`) pra nao perder o dado. Imprime
    aviso claro orientando o operador a fechar o Excel.

    Retorna o Path REALMENTE escrito (pode ser o backup).

    Causa tipica do erro: o operador abriu o XLSX no Excel pra olhar -
    isso cria lock NTFS que bloqueia `openpyxl.save`. PowerShell devolve
    OSError Errno 22 ("Invalid argument") em vez do mais claro
    PermissionError; tratamos os 2.
    """
    caminho_xlsx = Path(caminho_xlsx)
    erro_ultimo = None
    for tentativa in range(1, max_tentativas + 1):
        try:
            wb.save(str(caminho_xlsx))
            return caminho_xlsx
        except (OSError, PermissionError) as e:
            erro_ultimo = e
            if tentativa < max_tentativas:
                print(f"  (xlsx locked tentativa {tentativa}/{max_tentativas}): "
                      f"{type(e).__name__}: {e}. Aguardando {intervalo_s}s "
                      f"(feche o Excel se tiver aberto)...")
                time.sleep(intervalo_s)
            else:
                # Fallback: salva em backup
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                stem = caminho_xlsx.stem
                backup = caminho_xlsx.with_name(f"{stem}_backup_{ts}.xlsx")
                try:
                    wb.save(str(backup))
                    print(f"  AVISO: {caminho_xlsx.name} continua locked. "
                          f"Salvo em backup: {backup.name}")
                    print(f"  AVISO: feche o Excel e merge {backup.name} -> {caminho_xlsx.name} depois.")
                    return backup
                except Exception as e2:
                    print(f"  ERRO: nao consegui salvar nem em backup: {type(e2).__name__}: {e2}")
                    raise erro_ultimo
    raise erro_ultimo  # nunca chega aqui mas satisfaz type checker


# Status canonicos
STATUS_OK = "OK"
STATUS_ERRO = "ERRO"
STATUS_JA_FECHADO = "JA_FECHADO"

_CABECALHO = [
    "CIV", "Cliente", "CNJ", "Estado",
    "Fluxo", "Status", "Mensagem", "Timestamp",
]


@dataclass
class ResultadoLinha:
    civ: str
    cliente: str
    cnj: str
    estado: str
    fluxo: str
    status: str          # OK | ERRO | JA_FECHADO
    mensagem: str
    timestamp: str = ""  # preenchido em registrar() se vier vazio


def carregar_workbook_seguro(caminho: Path):
    """Carrega um xlsx existente de forma resiliente.

    Se o arquivo estiver CORROMPIDO (ex.: run anterior interrompido no meio
    da gravacao -> BadZipFile 'Bad magic number'), renomeia pra
    <stem>_corrompido_<timestamp>.xlsx e retorna None, pro chamador recriar
    do zero. Assim um unico xlsx quebrado nao derruba o robo inteiro.

    Retorna o workbook se OK, ou None se nao existe / estava corrompido.
    """
    caminho = Path(caminho)
    if not caminho.exists():
        return None
    try:
        return openpyxl.load_workbook(str(caminho))
    except Exception as e:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup = caminho.with_name(f"{caminho.stem}_corrompido_{ts}{caminho.suffix}")
        try:
            caminho.rename(backup)
            print(f">>> AVISO: '{caminho.name}' estava corrompido "
                  f"({type(e).__name__}). Movido pra '{backup.name}' e "
                  f"recriando do zero.")
        except Exception:
            try:
                caminho.unlink()
                print(f">>> AVISO: '{caminho.name}' corrompido e removido; recriando.")
            except Exception:
                print(f">>> AVISO: '{caminho.name}' corrompido e nao consegui "
                      f"mover/remover - pode falhar ao salvar.")
        return None


def _garantir_workbook(caminho: Path):
    wb = carregar_workbook_seguro(caminho)
    if wb is not None:
        return wb, wb.active

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Controle"
    ws.append(_CABECALHO)
    for col_idx in range(1, len(_CABECALHO) + 1):
        ws.cell(row=1, column=col_idx).font = Font(bold=True)
    return wb, ws


def registrar(caminho_xlsx: Path, resultado: ResultadoLinha) -> None:
    """Adiciona uma linha ao controle.xlsx (cria o arquivo se nao existir).

    Idempotente em paralelo nao e (openpyxl nao tem lock); rodar um robo
    por vez.
    """
    caminho_xlsx = Path(caminho_xlsx)
    caminho_xlsx.parent.mkdir(parents=True, exist_ok=True)

    if not resultado.timestamp:
        resultado.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    wb, ws = _garantir_workbook(caminho_xlsx)
    ws.append([
        sanitize_xlsx(resultado.civ),
        sanitize_xlsx(resultado.cliente),
        sanitize_xlsx(resultado.cnj),
        sanitize_xlsx(resultado.estado),
        sanitize_xlsx(resultado.fluxo),
        sanitize_xlsx(resultado.status),
        sanitize_xlsx(resultado.mensagem),
        sanitize_xlsx(resultado.timestamp),
    ])
    salvar_workbook_com_retry(wb, caminho_xlsx)
    wb.close()


def escrever_relatorio_lote(
    pasta_destino: Path,
    fluxo: str,
    resultados: list,
    inicio: datetime,
    instrucao_acao_manual: str = None,
) -> Path:
    """Escreve `_LOTE_RELATORIO_{data}.txt` com sumario do batch.

    Formatado pro OPERADOR: a secao de "ACAO MANUAL" aparece no topo se
    houve erros, pra ninguem precisar cacar.

    Args:
      pasta_destino: pasta onde criar o arquivo.
      fluxo: nome da tarefa processada (ex.: "Capturar mensagem de acordo").
      resultados: lista de ResultadoLinha do lote.
      inicio: datetime do comeco do lote.
      instrucao_acao_manual: texto curto explicando o que fazer pra
        resolver as CIVs com erro. Se None, usa uma instrucao generica.
    """
    pasta_destino = Path(pasta_destino)
    pasta_destino.mkdir(parents=True, exist_ok=True)
    fim = datetime.now()
    duracao = fim - inicio
    duracao_str = str(duracao).split(".")[0]

    total = len(resultados)
    ok = sum(1 for r in resultados if r.status == STATUS_OK)
    erro = sum(1 for r in resultados if r.status == STATUS_ERRO)
    ja_fechado = sum(1 for r in resultados if r.status == STATUS_JA_FECHADO)

    if instrucao_acao_manual is None:
        instrucao_acao_manual = (
            f"Abrir a CIV manualmente no e-Xyon e completar a tarefa "
            f"'{fluxo}' diretamente."
        )

    linhas = []
    linhas.append("=" * 72)
    linhas.append(f"  RELATORIO DE LOTE - {fluxo}")
    linhas.append("=" * 72)
    linhas.append(f"  Inicio:   {inicio.strftime('%Y-%m-%d %H:%M:%S')}")
    linhas.append(f"  Fim:      {fim.strftime('%Y-%m-%d %H:%M:%S')}")
    linhas.append(f"  Duracao:  {duracao_str}")
    linhas.append("")
    linhas.append(f"  TOTAL processado:                      {total:>3}")
    linhas.append( "  --------------------------------------------")
    linhas.append(f"  [OK]         Processadas pelo robo:    {ok:>3}")
    linhas.append(f"  [JA_FECHADO] Ja estavam prontas:       {ja_fechado:>3}")
    linhas.append(f"  [ATENCAO]    Precisam de ACAO MANUAL:  {erro:>3}")
    linhas.append( "  --------------------------------------------")
    linhas.append("")

    if erro == 0 and total > 0:
        linhas.append("  >>> TUDO PROCESSADO. Nenhuma acao manual necessaria. <<<")
    elif erro > 0:
        plural = "s precisam" if erro > 1 else " precisa"
        linhas.append(f"  >>> ATENCAO: {erro} CIV{plural} de tratamento manual."
                      " Detalhes abaixo. <<<")
    linhas.append("")

    if erro > 0:
        linhas.append("=" * 72)
        linhas.append("  CIVS QUE PRECISAM DE ACAO MANUAL")
        linhas.append("=" * 72)
        linhas.append("")
        for idx, r in enumerate(
            (r for r in resultados if r.status == STATUS_ERRO), 1
        ):
            linhas.append(f"  [{idx}] {r.civ} - {r.cliente or '(cliente nao capturado)'}")
            if r.cnj:
                linhas.append(f"      CNJ:      {r.cnj}")
            if r.estado:
                linhas.append(f"      Estado:   {r.estado}")
            linhas.append(f"      Motivo:   {r.mensagem}")
            linhas.append(f"      A fazer:  {instrucao_acao_manual}")
            linhas.append("")

    if ja_fechado > 0:
        linhas.append("=" * 72)
        linhas.append("  CIVs JA PROCESSADAS (idempotencia)")
        linhas.append("=" * 72)
        linhas.append("")
        for r in resultados:
            if r.status == STATUS_JA_FECHADO:
                linhas.append(f"  {r.civ:<14} {(r.cliente or '')[:40]}")
        linhas.append("")

    if ok > 0:
        linhas.append("=" * 72)
        linhas.append("  CIVs PROCESSADAS COM SUCESSO PELO ROBO")
        linhas.append("=" * 72)
        linhas.append("")
        for r in resultados:
            if r.status == STATUS_OK:
                linhas.append(f"  {r.civ:<14} {(r.cliente or '')[:40]}")
        linhas.append("")

    linhas.append("=" * 72)
    linhas.append("  Detalhes acumulados (todos os lotes): controle.xlsx")
    linhas.append("=" * 72)

    ts = fim.strftime("%Y-%m-%d_%H-%M")
    arquivo = pasta_destino / f"_LOTE_RELATORIO_{ts}.txt"
    arquivo.write_text("\n".join(linhas), encoding="utf-8")
    return arquivo
