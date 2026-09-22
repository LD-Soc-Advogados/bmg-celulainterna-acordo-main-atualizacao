"""
Configuracao central do bmg-celulainterna-acordo.

Carrega variaveis do .env (URL, timeouts, planilha de input, pastas de
saida). Credenciais NAO ficam em arquivo - o login e manual no Edge
dedicado e o script conecta via CDP na porta 9222.
"""
from pathlib import Path
import os
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

if not ENV_PATH.exists():
    raise FileNotFoundError(
        f"\nArquivo .env nao encontrado em: {ENV_PATH}\n"
        f"Resolva com PowerShell:\n"
        f"    Copy-Item .env.example .env\n"
    )

load_dotenv(ENV_PATH)

EXYON_URL = os.getenv("EXYON_URL", "https://juridico.bancobmg.com.br/")
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "30000"))
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "msedge").strip().lower()

# Conexao CDP - Edge aberto pelo abrir-edge-bmg.bat
CDP_URL = "http://localhost:9222"

# Pastas do projeto
LOGS_DIR = ROOT / "logs"
DOWNLOADS_DIR = ROOT / os.getenv("DOWNLOADS_DIR", "downloads")
SAIDA_DIR = ROOT / "saida"
# Caminho legado (compatibilidade) - usado se main NAO passar um path datado
SAIDA_PLANILHA = ROOT / os.getenv("SAIDA_PLANILHA", "saida/mensagens_acordo.xlsx")
CONTROLE_XLSX = ROOT / "controle.xlsx"


def get_saida_financeiro_atual() -> "Path":
    """Retorna o path da planilha de Financeiro DA RODADA ATUAL com
    timestamp no nome. Cada execucao gera arquivo novo:
        saida/financeiro_YYYY-MM-DD_HH-MM.xlsx

    A idempotencia continua funcionando via ja_capturado_em_pasta() que
    varre TODOS os financeiro_*.xlsx da pasta saida/.
    """
    from datetime import datetime as _dt
    ts = _dt.now().strftime("%Y-%m-%d_%H-%M")
    return SAIDA_DIR / f"financeiro_{ts}.xlsx"


# Alias de compatibilidade (nome antigo apontava pra saida de mensagens).
get_saida_mensagens_atual = get_saida_financeiro_atual


def _resolver_planilha_input() -> Path:
    """Resolve o caminho da planilha de input.

    Prioridade:
      1. Var de ambiente PLANILHA_INPUT (relativa ou absoluta).
      2. Primeiro arquivo na raiz que casa com 'Planilha Envio de casos*.xlsx'
         (ignora locks do Office '~$*').

    Retorna o Path (pode nao existir - a checagem fica no chamador).
    """
    var = os.getenv("PLANILHA_INPUT", "").strip()
    if var:
        p = Path(var)
        return p if p.is_absolute() else ROOT / p

    # Ordena por data de modificacao DECRESCENTE - sempre pega a planilha
    # mais recente. Assim o operador so coloca o arquivo novo na pasta e
    # nao precisa renomear ou apagar os antigos.
    candidatos = [
        p for p in ROOT.glob("Planilha Envio de casos*.xlsx")
        if not p.name.startswith("~$")
    ]
    candidatos.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if candidatos:
        return candidatos[0]
    return ROOT / "Planilha Envio de casos.xlsx"


PLANILHA_INPUT = _resolver_planilha_input()
