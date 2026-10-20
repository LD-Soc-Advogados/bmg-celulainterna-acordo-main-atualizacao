"""
GUI tkinter pra disparar o robo bmg-celulainterna-acordo.

Operacao tipica (Andreza / Adriano):
  1. Abre o Edge BMG (abrir-edge-bmg.bat) e faz login manualmente
  2. Clica duas vezes em iniciar.bat (que chama esse script)
  3. Escolhe a planilha (ou usa a ultima usada)
  4. Escolhe o MODO (tudo / so mensagem / so downloads / so extracao)
  5. Clica INICIAR
  6. Acompanha o log em tempo real
  7. Quando termina, popup com contadores OK / JA_FECHADO / ATENCAO

Despacha pra main.py via subprocess. NAO tem dry-run (esse projeto nao
tem analise previa - vai direto pro lote).

Portado de bmg-workflow-closer/iniciar.py, adaptado para:
  - Entrypoint main.py (em vez de run_geral.py)
  - Modos via radiobuttons (tudo / so X)
  - Checkbox --forcar (pula idempotencia)
  - Parser do resumo final no formato deste projeto
    ("RESUMO GERAL", "[OK]", "[JA_FECHADO]", "[ATENCAO]")
"""
import os
import re
import shutil
import urllib.request
import zipfile
import tempfile
import stat
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from pathlib import PurePosixPath
from tkinter import filedialog, messagebox, scrolledtext

try:
    import winsound
    _TEM_WINSOUND = True
except ImportError:
    _TEM_WINSOUND = False


ROOT = Path(__file__).resolve().parent


def _atualizar_por_zip(raiz_instalacao: Path) -> list:
    """Atualiza uma copia sem .git baixando somente arquivos do repo.

    Preserva configuracao, credenciais, planilhas, logs e saidas locais.
    """
    repositorio = "LD-Soc-Advogados/bmg-celulainterna-acordo-main-atualizacao"
    pacote = "bmg-celulainterna-acordo-main-atualizacao"
    url = f"https://github.com/{repositorio}/archive/refs/heads/main.zip"
    requisicao = urllib.request.Request(url, headers={"User-Agent": "BMG-CelulaInternaAcordo-Updater"})
    raiz_instalacao = Path(raiz_instalacao).resolve()
    protegidos = {
        ".git", ".venv", "venv", "downloads", "logs", "saida",
        ".cna_profile", ".cna_profile_chromium", "__pycache__",
    }
    arquivos_locais = {
        ".env", ".last_planilha.txt", "controle.xlsx",
        "Planilha Envio de casos.xlsx", "blacklist_advogados.txt",
        "lista_agressores.txt",
    }

    with tempfile.TemporaryDirectory(prefix="bmg-atualizacao-") as temp:
        temporario = Path(temp)
        zip_path = temporario / "main.zip"
        with urllib.request.urlopen(requisicao, timeout=60) as resposta:
            zip_path.write_bytes(resposta.read())

        staged = temporario / "arquivos"
        staged.mkdir()
        with zipfile.ZipFile(zip_path) as arquivo_zip:
            encontrados = 0
            for item in arquivo_zip.infolist():
                partes = PurePosixPath(item.filename).parts
                try:
                    pos_pacote = partes.index(pacote)
                except ValueError:
                    continue
                partes_relativas = partes[pos_pacote + 1:]
                if item.is_dir() or not partes_relativas or ".." in partes_relativas:
                    continue
                if any(parte in protegidos for parte in partes_relativas):
                    continue
                relativo = PurePosixPath(*partes_relativas)
                if relativo.name in arquivos_locais or relativo.name.startswith("~$"):
                    continue
                if relativo.suffix.lower() in {".xlsx", ".xlsm", ".log", ".pyc", ".pyo"}:
                    continue
                modo_unix = item.external_attr >> 16
                if stat.S_ISLNK(modo_unix):
                    continue
                destino = staged.joinpath(*partes_relativas)
                destino.parent.mkdir(parents=True, exist_ok=True)
                with arquivo_zip.open(item) as origem, destino.open("wb") as saida:
                    shutil.copyfileobj(origem, saida)
                encontrados += 1

        if not encontrados:
            raise RuntimeError("O pacote baixado nao contem os arquivos esperados do projeto.")

        backups = {}
        criados = []
        atualizados = []
        try:
            for origem in staged.rglob("*"):
                if not origem.is_file():
                    continue
                relativo = origem.relative_to(staged)
                destino = raiz_instalacao / relativo
                destino.parent.mkdir(parents=True, exist_ok=True)
                if destino.exists() and destino.read_bytes() == origem.read_bytes():
                    continue
                if destino.exists():
                    backup = temporario / "backup" / relativo
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(destino, backup)
                    backups[destino] = backup
                else:
                    criados.append(destino)
                shutil.copy2(origem, destino)
                atualizados.append(relativo.as_posix())
        except Exception:
            for destino in criados:
                try:
                    destino.unlink(missing_ok=True)
                except Exception:
                    pass
            for destino, backup in backups.items():
                try:
                    shutil.copy2(backup, destino)
                except Exception:
                    pass
            raise

    return sorted(atualizados)


def _resumo_atualizacao_zip(arquivos: list) -> str:
    if not arquivos:
        return "Esta pasta ja esta atualizada. Nenhum arquivo precisou ser alterado."
    linhas = [f"Arquivos atualizados: {len(arquivos)}."]
    if "requirements.txt" in arquivos:
        linhas.append("As dependencias mudaram. Rode setup.ps1 antes de iniciar o robo.")
    if "iniciar.py" in arquivos:
        linhas.append("Feche e abra o iniciador novamente para carregar a versao nova.")
    else:
        linhas.append("Voce pode usar o robo normalmente.")
    return "\n\n".join(linhas)


VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
MAIN_PY = ROOT / "main.py"
LAST_PLANILHA_FILE = ROOT / ".last_planilha.txt"
CONTROLE_XLSX = ROOT / "controle.xlsx"
SAIDA_DIR = ROOT / "saida"
LOGS_DIR = ROOT / "logs"
EDGE_BAT = ROOT / "abrir-edge-bmg.bat"
BLACKLIST_TXT = ROOT / "blacklist_advogados.txt"
AGRESSORES_TXT = ROOT / "lista_agressores.txt"
ENV_FILE = ROOT / ".env"

DIAS_LIMPEZA = 30  # pastas de download com mtime mais antigo sao candidatas


def _ler_downloads_dir() -> Path:
    """Le DOWNLOADS_DIR do .env (sem importar o core, que puxa playwright).
    Resolve relativo a ROOT. Default 'downloads'."""
    valor = "downloads"
    try:
        for linha in ENV_FILE.read_text(encoding="utf-8").splitlines():
            linha = linha.strip()
            if linha.startswith("DOWNLOADS_DIR="):
                valor = linha.split("=", 1)[1].strip().strip('"').strip("'")
                break
    except Exception:
        pass
    p = Path(valor)
    return p if p.is_absolute() else ROOT / p


def _eh_drive_de_rede(p: Path) -> bool:
    """True se `p` esta em UNC (\\\\server\\share) ou letra mapeada de rede.
    A Lixeira do Windows nao funciona em drive de rede - o Windows apaga
    permanente mesmo pedindo 'mandar pra Lixeira'."""
    s = str(p)
    if s.startswith("\\\\"):
        return True
    if len(s) >= 2 and s[1] == ":":
        try:
            import ctypes
            # GetDriveTypeW: DRIVE_REMOTE = 4
            return ctypes.windll.kernel32.GetDriveTypeW(s[0] + ":\\") == 4
        except Exception:
            return False
    return False


# ============================================================================
# CHECAGENS PRE-INICIO
# ============================================================================
def _checar_cdp_edge() -> tuple:
    """(ok, mensagem). Verifica se o Edge BMG esta aberto na porta 9222."""
    import urllib.request
    import json
    try:
        with urllib.request.urlopen("http://localhost:9222/json/list", timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return (False, "O Edge BMG nao esta aberto.\n\n"
                       "Clique em 'Abrir Edge BMG' (ou em abrir-edge-bmg.bat na pasta), "
                       "faca login no e-Xyon, e volte aqui.")
    abas_bmg = [t for t in data if "bancobmg.com.br" in t.get("url", "")]
    if not abas_bmg:
        return (False, "O Edge esta aberto mas sem aba do BMG.\n\n"
                       "Va no e-Xyon do BMG, faca login, e espere chegar na tela principal.")
    abas_logadas = [t for t in abas_bmg if "_FSet_Main" in t.get("url", "")]
    if not abas_logadas:
        return (False, "Voce esta no e-Xyon mas ainda nao fez login.\n\n"
                       "Faca login (usuario, senha, captcha) e espere chegar na tela principal.")
    return (True, "")


def _checar_edge_aberto() -> tuple:
    """(ok, mensagem). Verifica SO se o Edge dedicado esta na porta 9222,
    sem exigir e-Xyon/login. Usado pelo CNA (site publico, so precisa do
    navegador real pro reCAPTCHA)."""
    import urllib.request
    try:
        urllib.request.urlopen("http://localhost:9222/json/list", timeout=2)
        return (True, "")
    except Exception:
        return (False, "O Edge dedicado nao esta aberto.\n\n"
                       "Clique em 'Abrir Edge BMG' (ou abrir-edge-bmg.bat). "
                       "Para o CNA NAO precisa fazer login no e-Xyon - so deixar "
                       "o Edge aberto.")


def _checar_controle_fechado() -> tuple:
    """(ok, mensagem). controle.xlsx nao pode estar aberto no Excel."""
    if not CONTROLE_XLSX.exists():
        return (True, "")
    try:
        f = open(CONTROLE_XLSX, "a")
        f.close()
        return (True, "")
    except (OSError, PermissionError):
        return (False, "O arquivo 'controle.xlsx' esta aberto no Excel.\n\n"
                       "Feche ele antes de continuar.")


def _checar_planilha(caminho: str, modo_offline: bool = False) -> tuple:
    """(ok, mensagem). Modos --so-extracao nao exigem planilha valida."""
    if modo_offline and not caminho:
        return (True, "")  # extracao pode rodar sem planilha (modo --civ)
    if not caminho:
        return (False, "Escolha uma planilha primeiro clicando em 'Procurar'.")
    p = Path(caminho)
    if not p.exists():
        return (False, f"Arquivo nao encontrado:\n{caminho}")
    if p.suffix.lower() != ".xlsx":
        return (False, f"Esperava .xlsx, recebi {p.suffix}.\n\n"
                       "Se for .xls antigo, abra no Excel e salve como .xlsx primeiro.")
    try:
        f = open(p, "r+b")
        f.close()
    except (OSError, PermissionError):
        return (False, f"A planilha esta aberta no Excel (ou em outro programa).\n\n"
                       f"Feche ela antes de continuar:\n{p.name}")
    return (True, "")


# ============================================================================
# CLASSE PRINCIPAL DA APLICACAO
# ============================================================================
class App:
    def __init__(self, root):
        self.root = root
        self.processo = None
        self.thread_leitor = None

        root.title("Robo BMG - Acordo (Celula Interna)")
        root.geometry("900x720")
        root.minsize(750, 580)

        # Tema vermelho (Lucchesi Dolabela)
        self.cor_marca = "#F23A3A"

        self._criar_widgets()
        self._carregar_ultima_planilha()

    # ---- UI ----------------------------------------------------------------
    def _criar_widgets(self):
        # ===== CABECALHO =====
        frame_header = tk.Frame(self.root, bg="white")
        frame_header.pack(fill=tk.X, padx=10, pady=(10, 0))
        tk.Label(frame_header,
                 text="Robo BMG - Acordo (Celula Interna)",
                 bg="white", fg=self.cor_marca,
                 font=("", 13, "bold")).pack(side=tk.LEFT, anchor="w")
        tk.Label(frame_header,
                 text="Lucchesi Dolabela Soc. Advogados",
                 bg="white", fg="#666",
                 font=("", 9)).pack(side=tk.RIGHT, anchor="e")

        # ===== PLANILHA =====
        frame_top = tk.LabelFrame(self.root, text=" Planilha de entrada ",
                                  padx=10, pady=10, font=("", 10, "bold"))
        frame_top.pack(fill=tk.X, padx=10, pady=(10, 5))
        frame_planilha = tk.Frame(frame_top)
        frame_planilha.pack(fill=tk.X)
        self.entry_planilha = tk.Entry(frame_planilha, font=("", 10))
        self.entry_planilha.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        self.btn_procurar = tk.Button(frame_planilha, text="Procurar...",
                                       command=self.procurar, padx=10)
        self.btn_procurar.pack(side=tk.LEFT, padx=(5, 0))

        # ===== MODO + OPCOES =====
        frame_modo = tk.LabelFrame(self.root, text=" Modo de execucao ",
                                    padx=10, pady=10, font=("", 10, "bold"))
        frame_modo.pack(fill=tk.X, padx=10, pady=5)

        self.modo_var = tk.StringVar(value="completo")
        modos = [
            ("completo",   "COMPLETO: Tarefa 1 + 2 + advogados (DJEN) + contato (CNA) - tudo de uma vez"),
            ("tudo",       "Tudo: Tarefa 1 (Financeiro) + Tarefa 2 (downloads)"),
            ("so_mensagem","So Tarefa 1: extrair dados da aba Financeiro"),
            ("so_downloads","So Tarefa 2: baixar documentos (PDFs)"),
            ("so_extracao","So Tarefa 3: extrair contatos dos PDFs ja baixados (offline)"),
            ("so_djen",    "So Tarefa 4: advogados + OAB via DJEN (API publica, sem Edge)"),
            ("so_cna",     "So Tarefa 5: contato (tel/endereco) via CNA - aceita a saida da Tarefa 4 OU a planilha do Banco (roda o DJEN antes)"),
            ("so_manifesto","So gerar o manifesto (CIV/Cliente/CNJ/Pasta) da planilha - pro Cowork"),
        ]
        for valor, label in modos:
            negrito = ("", 9, "bold") if valor == "completo" else ("", 9)
            tk.Radiobutton(frame_modo, text=label, variable=self.modo_var,
                            value=valor, anchor="w", font=negrito
                            ).pack(fill=tk.X, anchor="w")

        # Opcoes extras
        frame_opts = tk.Frame(frame_modo, pady=5)
        frame_opts.pack(fill=tk.X, anchor="w")
        self.forcar_var = tk.BooleanVar(value=False)
        tk.Checkbutton(frame_opts,
                        text="--forcar (reprocessa CIVs ja capturadas - util pra retomar lote falho)",
                        variable=self.forcar_var, font=("", 9)
                        ).pack(anchor="w")

        self.civ_unica_var = tk.StringVar(value="")
        frame_civ = tk.Frame(frame_modo)
        frame_civ.pack(fill=tk.X, anchor="w", pady=(3, 0))
        tk.Label(frame_civ, text="Testar UMA CIV especifica (opcional):",
                 font=("", 9)).pack(side=tk.LEFT)
        tk.Entry(frame_civ, textvariable=self.civ_unica_var,
                 width=18, font=("", 9)).pack(side=tk.LEFT, padx=5)
        tk.Label(frame_civ, text="(ex: CIV1579287)",
                 font=("", 8), fg="#888").pack(side=tk.LEFT)

        # ===== BOTOES PRINCIPAIS =====
        frame_botoes = tk.Frame(self.root, pady=10)
        frame_botoes.pack()
        self.btn_iniciar = tk.Button(frame_botoes, text="INICIAR ROBO",
                                      command=self.iniciar,
                                      font=("", 12, "bold"),
                                      bg="#4CAF50", fg="white",
                                      activebackground="#45a049", activeforeground="white",
                                      padx=30, pady=10, cursor="hand2")
        self.btn_iniciar.pack(side=tk.LEFT, padx=5)
        self.btn_parar = tk.Button(frame_botoes, text="Parar",
                                    command=self.parar, state=tk.DISABLED,
                                    padx=20, pady=10, cursor="hand2")
        self.btn_parar.pack(side=tk.LEFT, padx=5)

        # Botoes secundarios
        frame_aux = tk.Frame(self.root)
        frame_aux.pack()
        tk.Button(frame_aux, text="Abrir Edge BMG",
                   command=self.abrir_edge, padx=10
                   ).pack(side=tk.LEFT, padx=3)
        tk.Button(frame_aux, text="Pasta de saida",
                   command=self.abrir_saida, padx=10
                   ).pack(side=tk.LEFT, padx=3)
        tk.Button(frame_aux, text="Pasta de logs",
                   command=self.abrir_logs, padx=10
                   ).pack(side=tk.LEFT, padx=3)
        tk.Button(frame_aux, text="Blacklist advogados",
                   command=self.editar_blacklist, padx=10
                   ).pack(side=tk.LEFT, padx=3)
        tk.Button(frame_aux, text="Lista de Agressores",
                   command=self.editar_agressores, padx=10
                   ).pack(side=tk.LEFT, padx=3)
        tk.Button(frame_aux, text=f"Limpar downloads +{DIAS_LIMPEZA}d",
                   command=self.limpar_downloads_antigos, padx=10
                   ).pack(side=tk.LEFT, padx=3)
        self.btn_atualizar = tk.Button(
            frame_aux, text="Atualizar do GitHub",
            command=self.atualizar_do_github, padx=10,
            bg="#1976D2", fg="white",
            activebackground="#1565C0", activeforeground="white",
        )
        self.btn_atualizar.pack(side=tk.LEFT, padx=3)

        # ===== LOG =====
        frame_log = tk.LabelFrame(self.root, text=" Log de execucao ",
                                   padx=10, pady=10, font=("", 10, "bold"))
        frame_log.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 10))
        self.text_log = scrolledtext.ScrolledText(frame_log, state=tk.DISABLED,
                                                   wrap=tk.NONE,
                                                   font=("Consolas", 9), bg="#1e1e1e",
                                                   fg="#d4d4d4", insertbackground="white")
        self.text_log.pack(fill=tk.BOTH, expand=True)

    # ---- Persistencia da ultima planilha ----------------------------------
    def _carregar_ultima_planilha(self):
        if LAST_PLANILHA_FILE.exists():
            try:
                caminho = LAST_PLANILHA_FILE.read_text(encoding="utf-8").strip()
                if caminho and Path(caminho).exists():
                    self.entry_planilha.insert(0, caminho)
                    return
            except Exception:
                pass
        # Default: pega a Planilha mais recente da raiz se houver
        candidatas = sorted(ROOT.glob("Planilha Envio de casos*.xlsx"),
                             key=lambda p: p.stat().st_mtime, reverse=True)
        if candidatas:
            self.entry_planilha.insert(0, str(candidatas[0]))

    def _salvar_ultima_planilha(self, caminho: str):
        try:
            LAST_PLANILHA_FILE.write_text(caminho, encoding="utf-8")
        except Exception:
            pass

    # ---- Acoes botoes ------------------------------------------------------
    def procurar(self):
        atual = self.entry_planilha.get().strip()
        initial = str(Path(atual).parent) if atual and Path(atual).parent.exists() else str(ROOT)
        caminho = filedialog.askopenfilename(
            title="Selecione a planilha do Banco BMG",
            initialdir=initial,
            filetypes=[("Planilhas Excel", "*.xlsx"), ("Todos arquivos", "*.*")],
        )
        if caminho:
            self.entry_planilha.delete(0, tk.END)
            self.entry_planilha.insert(0, caminho)
            self._salvar_ultima_planilha(caminho)

    def abrir_edge(self):
        if not EDGE_BAT.exists():
            messagebox.showerror("Erro", f"Nao achei: {EDGE_BAT}")
            return
        try:
            os.startfile(str(EDGE_BAT))
        except Exception as e:
            messagebox.showerror("Erro", f"Nao consegui abrir:\n{e}")

    def abrir_saida(self):
        SAIDA_DIR.mkdir(exist_ok=True)
        try:
            os.startfile(str(SAIDA_DIR))
        except Exception as e:
            messagebox.showerror("Erro", f"Nao consegui abrir:\n{e}")

    def abrir_logs(self):
        LOGS_DIR.mkdir(exist_ok=True)
        try:
            os.startfile(str(LOGS_DIR))
        except Exception as e:
            messagebox.showerror("Erro", f"Nao consegui abrir:\n{e}")

    def limpar_downloads_antigos(self):
        """Apaga as pastas de download com mais de DIAS_LIMPEZA dias
        (por data de modificacao). Lista e pede confirmacao antes.

        Estrategia:
          - Local (C:\\...):  usa send2trash (recuperavel via Lixeira).
          - Rede (X:\\..., UNC): Lixeira do Windows NAO funciona em drive
            de rede - shutil.rmtree direto, com dupla confirmacao.
          - Se send2trash indisponivel: mesmo caminho de rmtree (com dupla
            confirmacao); a mensagem diz qual Python esta em uso pra o
            operador conferir se abriu a GUI fora da venv.
        """
        import shutil as _shutil
        import sys as _sys
        import time as _time

        try:
            from send2trash import send2trash
            _tem_send2trash = True
        except ImportError:
            send2trash = None  # noqa: F841
            _tem_send2trash = False

        base = _ler_downloads_dir()
        if not base.exists():
            messagebox.showinfo("Limpar downloads",
                                f"A pasta de downloads nao existe:\n{base}")
            return

        em_rede = _eh_drive_de_rede(base)

        limite = _time.time() - DIAS_LIMPEZA * 86400
        antigas = []
        for sub in base.iterdir():
            try:
                if sub.is_dir() and sub.stat().st_mtime < limite:
                    dias = int((_time.time() - sub.stat().st_mtime) / 86400)
                    antigas.append((sub, dias))
            except Exception:
                continue

        if not antigas:
            messagebox.showinfo(
                "Limpar downloads",
                f"Nenhuma pasta com mais de {DIAS_LIMPEZA} dias em:\n{base}")
            return

        antigas.sort(key=lambda x: x[1], reverse=True)
        amostra = "\n".join(f"  - {p.name} ({d} dias)" for p, d in antigas[:15])
        if len(antigas) > 15:
            amostra += f"\n  ... e mais {len(antigas) - 15} pasta(s)"

        # Decide o metodo: Lixeira so em drive LOCAL com send2trash.
        usar_permanente = em_rede or not _tem_send2trash
        if usar_permanente:
            if em_rede:
                motivo = ("A pasta de destino esta em drive de REDE "
                          "(a Lixeira do Windows nao funciona em rede)")
            else:
                motivo = (f"O pacote 'send2trash' nao esta disponivel no "
                          f"Python em uso ({_sys.executable}).\n\n"
                          f"Se voce abriu a GUI pelo 'iniciar.bat', ele usa "
                          f"a venv do projeto (.venv). Se abriu diretamente "
                          f"o iniciar.py, esta usando o Python do sistema, "
                          f"que nao tem as dependencias do robo instaladas")
            if not messagebox.askyesno(
                    "Apagar PERMANENTE?",
                    f"{motivo}.\n\n"
                    f"As {len(antigas)} pasta(s) abaixo serao apagadas de "
                    f"forma PERMANENTE (nao vao pra Lixeira; nao da pra "
                    f"recuperar).\n\n{amostra}\n\n"
                    f"Pasta base:\n{base}\n\nConfirma?"):
                return
        else:
            if not messagebox.askyesno(
                    "Confirmar limpeza",
                    f"{len(antigas)} pasta(s) com mais de {DIAS_LIMPEZA} dias "
                    f"serao enviadas para a LIXEIRA do Windows (recuperavel):"
                    f"\n\n{amostra}\n\nPasta base:\n{base}\n\nConfirma?"):
                return

        ok, falhas = 0, []
        for p, _d in antigas:
            try:
                if usar_permanente:
                    _shutil.rmtree(str(p))
                else:
                    send2trash(str(p))
                ok += 1
            except Exception as e:
                falhas.append(f"{p.name}: {e}")

        if usar_permanente:
            msg = f"{ok} pasta(s) apagada(s) PERMANENTEMENTE."
        else:
            msg = f"{ok} pasta(s) enviada(s) para a Lixeira."
        if falhas:
            msg += f"\n\n{len(falhas)} falha(s):\n" + "\n".join(falhas[:8])
        messagebox.showinfo("Limpeza concluida", msg)

    def _editar_lista_txt(self, caminho, titulo, ajuda):
        """Editor generico de arquivo-lista (blacklist / agressores)."""
        conteudo = ""
        if caminho.exists():
            try:
                conteudo = caminho.read_text(encoding="utf-8")
            except Exception as e:
                messagebox.showerror("Erro", f"Nao consegui ler o arquivo:\n{e}")
                return

        win = tk.Toplevel(self.root)
        win.title(titulo)
        win.geometry("720x520")
        win.transient(self.root)

        tk.Label(win, text=ajuda, justify=tk.LEFT, anchor="w",
                 fg="#444", font=("", 9)).pack(fill=tk.X, padx=10, pady=(10, 5))

        txt = scrolledtext.ScrolledText(win, wrap=tk.NONE, font=("Consolas", 10))
        txt.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        txt.insert("1.0", conteudo)

        frame_btn = tk.Frame(win, pady=8)
        frame_btn.pack()

        def salvar():
            novo = txt.get("1.0", tk.END)
            try:
                caminho.write_text(novo.rstrip("\n") + "\n", encoding="utf-8")
            except Exception as e:
                messagebox.showerror("Erro", f"Nao consegui salvar:\n{e}")
                return
            messagebox.showinfo(
                "Salvo",
                "Lista salva.\n\nVale a partir da PROXIMA execucao da Tarefa 4 "
                "(o robo le o arquivo ao iniciar cada rodada).",
            )
            win.destroy()

        tk.Button(frame_btn, text="Salvar", command=salvar,
                   bg="#4CAF50", fg="white", padx=20, pady=5,
                   activebackground="#45a049", activeforeground="white",
                   cursor="hand2").pack(side=tk.LEFT, padx=5)
        tk.Button(frame_btn, text="Abrir no Bloco de Notas",
                   command=lambda: os.startfile(str(caminho)) if caminho.exists() else None,
                   padx=12, pady=5).pack(side=tk.LEFT, padx=5)
        tk.Button(frame_btn, text="Cancelar", command=win.destroy,
                   padx=20, pady=5).pack(side=tk.LEFT, padx=5)

    def editar_blacklist(self):
        self._editar_lista_txt(
            BLACKLIST_TXT, "Blacklist de advogados",
            ("Advogados aqui NUNCA sao tratados como advogado do Autor "
             "(Tarefa 4 / DJEN).\nFormato por linha:  NOME PARCIAL | OAB | UF "
             "  (basta casar nome OU OAB).\nLinhas com # sao comentario.\n"
             "CUIDADO: nao inclua advogado de Autor com muitos processos "
             "contra o BMG."))

    def editar_agressores(self):
        self._editar_lista_txt(
            AGRESSORES_TXT, "Lista de Agressores",
            ("Advogados aqui recebem SIM na coluna 'Na_Lista_Agressores' da "
             "saida do DJEN (Tarefa 4).\nNAO sao excluidos - apenas "
             "SINALIZADOS pra conferencia.\nFormato por linha:  NOME PARCIAL "
             "| OAB | UF  (basta casar nome OU OAB).\nLinhas com # sao "
             "comentario."))

    def atualizar_do_github(self):
        """Roda `git pull` na pasta do projeto e mostra resultado em popup.

        Detecta:
          - "Already up to date" -> popup verde
          - Atualizacao bem-sucedida -> popup com lista de arquivos modificados
          - requirements.txt mudou -> recomenda rodar setup.ps1
          - iniciar.py mudou -> recomenda fechar/reabrir GUI
          - Erro (rede, conflito, etc.) -> popup com stderr

        Roda no thread principal (operacao curta, ~2s). Desabilita o
        botao durante a chamada.
        """
        self.btn_atualizar.config(state=tk.DISABLED, text="Atualizando...")
        self.root.update_idletasks()

        try:
            resultado = subprocess.run(
                ["git", "pull", "--ff-only"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=str(ROOT), timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except FileNotFoundError:
            try:
                arquivos = _atualizar_por_zip(ROOT)
            except Exception as erro:
                self.btn_atualizar.config(state=tk.NORMAL, text="Atualizar do GitHub")
                messagebox.showerror("Erro ao atualizar", f"Nao consegui baixar a atualizacao.\n\n{type(erro).__name__}: {erro}")
                return
            self.btn_atualizar.config(state=tk.NORMAL, text="Atualizar do GitHub")
            messagebox.showinfo("Atualizacao concluida", _resumo_atualizacao_zip(arquivos))
            return
        except subprocess.TimeoutExpired:
            self.btn_atualizar.config(state=tk.NORMAL, text="Atualizar do GitHub")
            messagebox.showerror(
                "Timeout",
                "git pull demorou mais de 60s. Verifique sua conexao com a internet.",
            )
            return
        except Exception as e:
            self.btn_atualizar.config(state=tk.NORMAL, text="Atualizar do GitHub")
            messagebox.showerror("Erro", f"{type(e).__name__}: {e}")
            return

        self.btn_atualizar.config(state=tk.NORMAL, text="Atualizar do GitHub")

        saida = (resultado.stdout or "") + (resultado.stderr or "")

        # Caso 1: erro no git pull (rede, auth, conflito)
        if resultado.returncode != 0:
            if "not a git repository" in saida.lower():
                try:
                    arquivos = _atualizar_por_zip(ROOT)
                except Exception as erro:
                    messagebox.showerror("Erro ao atualizar", f"Esta pasta nao tem Git e o download alternativo falhou.\n\n{type(erro).__name__}: {erro}")
                    return
                messagebox.showinfo("Atualizacao concluida", _resumo_atualizacao_zip(arquivos))
                return
            messagebox.showerror(
                "Erro ao atualizar",
                f"git pull falhou (codigo {resultado.returncode}).\n\n"
                f"Saida:\n{saida.strip()[:800]}\n\n"
                "Possiveis causas: sem internet, conflito de arquivos, "
                "ou autenticacao expirada (rode 'gh auth login' no PowerShell).",
            )
            return

        # Caso 2: ja estava atualizado
        if "Already up to date" in saida or "Already up-to-date" in saida:
            messagebox.showinfo(
                "Ja atualizado",
                "Voce ja esta na ultima versao do GitHub.\n\n"
                "Nada mudou desde a sua ultima atualizacao.",
            )
            return

        # Caso 3: atualizou - parsea arquivos modificados
        # Saida tipica:
        #   Updating abc123..def456
        #   Fast-forward
        #    iniciar.py        | 42 +++++---
        #    core/processo.py  |  5 +++--
        #    2 files changed, 35 insertions(+), 12 deletions(-)
        arquivos = []
        for linha in saida.splitlines():
            l = linha.strip()
            if "|" in l and not l.startswith("|"):
                nome = l.split("|")[0].strip()
                if nome and not nome.startswith("("):
                    arquivos.append(nome)

        # Avisos extras se arquivos sensiveis mudaram
        avisos = []
        if any("requirements.txt" in a for a in arquivos):
            avisos.append(
                "* requirements.txt mudou - rode setup.ps1 de novo no PowerShell "
                "pra instalar as novas dependencias:\n"
                "    powershell -ExecutionPolicy Bypass -File setup.ps1"
            )
        if any("iniciar.py" in a for a in arquivos):
            avisos.append(
                "* iniciar.py mudou - feche esta janela e abra de novo "
                "(duplo-clique no iniciar.bat) pra usar a versao nova."
            )
        if any(a.startswith("setup.ps1") for a in arquivos):
            avisos.append(
                "* setup.ps1 mudou - rode de novo no PowerShell:\n"
                "    powershell -ExecutionPolicy Bypass -File setup.ps1"
            )

        msg_arquivos = "\n".join(f"  - {a}" for a in arquivos[:15])
        if len(arquivos) > 15:
            msg_arquivos += f"\n  ... e mais {len(arquivos) - 15} arquivo(s)"

        msg = f"Atualizacao concluida!\n\nArquivos modificados ({len(arquivos)}):\n{msg_arquivos}"
        if avisos:
            msg += "\n\nATENCAO:\n" + "\n\n".join(avisos)
        else:
            msg += "\n\nVoce pode usar o robo normalmente."

        messagebox.showinfo("Atualizado", msg)

    def iniciar(self):
        modo = self.modo_var.get()
        caminho = self.entry_planilha.get().strip()
        civ_unica = self.civ_unica_var.get().strip()
        forcar = self.forcar_var.get()

        # CNA (Tarefa 5) le a saida do DJEN (nao precisa de planilha) e abre
        # o PROPRIO navegador limpo - NAO precisa do Edge dedicado.
        modo_cna = (modo == "so_cna")
        # Modos que dispensam o Edge dedicado: so_extracao (PDFs locais),
        # so_djen (HTTP), so_cna (navegador proprio) e so_manifesto (so xlsx).
        modo_sem_edge = modo in ("so_extracao", "so_djen", "so_cna", "so_manifesto")

        # Pre-checks: planilha (so_extracao/so_djen aceitam planilha;
        # so_cna nao precisa de nenhuma).
        if not civ_unica and not modo_cna:
            ok, msg = _checar_planilha(caminho, modo_offline=modo_sem_edge)
            if not ok:
                messagebox.showwarning("Planilha", msg)
                return

        ok, msg = _checar_controle_fechado()
        if not ok:
            messagebox.showwarning("Controle aberto", msg)
            return

        # Edge dedicado: necessario so pras tarefas do e-Xyon (T1/T2/completo).
        # so_extracao/so_djen/so_cna nao precisam dele.
        if not modo_sem_edge:
            ok, msg = _checar_cdp_edge()
            if not ok:
                resposta = messagebox.askyesno("Edge BMG", msg + "\n\nQuer abrir o Edge agora?")
                if resposta:
                    self.abrir_edge()
                return

        # Salva planilha como ultima (se houve)
        if caminho:
            self._salvar_ultima_planilha(caminho)

        # Monta args
        cmd = [str(VENV_PYTHON), "-u", str(MAIN_PY)]
        if civ_unica:
            cmd += ["--civ", civ_unica]
        elif caminho:
            cmd += ["--planilha", caminho]
        if modo == "completo":
            cmd += ["--completo"]
        elif modo == "so_mensagem":
            cmd += ["--so-mensagem"]
        elif modo == "so_downloads":
            cmd += ["--so-downloads"]
        elif modo == "so_extracao":
            cmd += ["--so-extracao"]
        elif modo == "so_djen":
            cmd += ["--so-djen"]
        elif modo == "so_cna":
            cmd += ["--so-cna"]
        elif modo == "so_manifesto":
            cmd += ["--so-manifesto"]
        if forcar:
            cmd += ["--forcar"]

        # Desabilita controles
        self.btn_iniciar.config(state=tk.DISABLED)
        self.btn_procurar.config(state=tk.DISABLED)
        self.btn_parar.config(state=tk.NORMAL)
        self.btn_atualizar.config(state=tk.DISABLED)
        self.entry_planilha.config(state=tk.DISABLED)

        self._clear_log()
        self._append_log(f">>> Comando: {' '.join(cmd)}\n\n")

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        try:
            self.processo = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                cwd=str(ROOT), env=env, bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except Exception as e:
            messagebox.showerror("Erro ao iniciar", f"{type(e).__name__}: {e}")
            self._reativar_controles()
            return

        self.thread_leitor = threading.Thread(target=self._ler_output, daemon=True)
        self.thread_leitor.start()

    def parar(self):
        if not self.processo or self.processo.poll() is not None:
            return
        if not messagebox.askyesno("Parar robo?",
                                    "Tem certeza que quer parar o robo agora?\n\n"
                                    "As CIVs ja processadas estao salvas. As proximas serao puladas."):
            return
        try:
            self.processo.terminate()
            try:
                self.processo.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.processo.kill()
            self._append_log("\n>>> ROBO INTERROMPIDO PELO USUARIO\n")
        except Exception as e:
            self._append_log(f"\n>>> Erro ao parar: {e}\n")

    # ---- Thread leitor de output -------------------------------------------
    def _ler_output(self):
        try:
            for linha in iter(self.processo.stdout.readline, ''):
                if linha:
                    self.root.after(0, self._append_log, linha)
        except Exception as e:
            self.root.after(0, self._append_log, f"\n[erro leitor]: {e}\n")
        try:
            self.processo.wait()
        except Exception:
            pass
        self.root.after(0, self._finalizar)

    def _finalizar(self):
        codigo = self.processo.returncode if self.processo else None
        self._reativar_controles()
        if _TEM_WINSOUND:
            try:
                winsound.MessageBeep(winsound.MB_OK)
            except Exception:
                pass

        log_text = self.text_log.get("1.0", tk.END)
        resumo = self._extrair_resumo_do_log(log_text)
        if resumo:
            self._popup_concluido(resumo)
        else:
            if codigo == 0:
                messagebox.showinfo("Concluido", "Robo terminou. Veja o log pra detalhes.")
            else:
                messagebox.showwarning("Terminou com aviso",
                                        f"Robo terminou com codigo {codigo}. Veja o log.")

    def _extrair_resumo_do_log(self, log: str) -> dict:
        """Extrai contadores OK/JA_FECHADO/ATENCAO do bloco 'RESUMO GERAL'
        no formato deste projeto:

            ========================================================================
              RESUMO GERAL (N CIVs, M resultados)
            ========================================================================
              [OK]         X
              [JA_FECHADO] Y
              [ATENCAO]    Z
        """
        ok = re.search(r"\[OK\]\s+(\d+)", log)
        ja = re.search(r"\[JA_FECHADO\]\s+(\d+)", log)
        atn = re.search(r"\[ATENCAO\]\s+(\d+)", log)
        rel = re.search(r"Relatorio do lote:\s*(\S+)", log)
        log_arquivo = re.search(r"Log desta execucao:\s*(\S+)", log)
        if not (ok or ja or atn):
            return None
        return {
            "ok": int(ok.group(1)) if ok else 0,
            "ja": int(ja.group(1)) if ja else 0,
            "atn": int(atn.group(1)) if atn else 0,
            "relatorio": rel.group(1) if rel else None,
            "log": log_arquivo.group(1) if log_arquivo else None,
        }

    def _popup_concluido(self, resumo: dict):
        win = tk.Toplevel(self.root)
        win.title("Concluido")
        win.geometry("460x360")
        win.transient(self.root)
        win.grab_set()
        win.update_idletasks()
        x = (win.winfo_screenwidth() // 2) - (460 // 2)
        y = (win.winfo_screenheight() // 2) - (360 // 2)
        win.geometry(f"+{x}+{y}")

        tk.Label(win, text="Concluido!", font=("", 16, "bold"),
                 fg="#2e7d32", pady=15).pack()
        frame = tk.Frame(win, pady=10)
        frame.pack()
        tk.Label(frame, text=f"OK         {resumo['ok']:>4}  -  capturados com sucesso",
                 font=("Consolas", 11), anchor="w").pack(anchor="w", padx=20, pady=2)
        tk.Label(frame, text=f"JA_FECHADO {resumo['ja']:>4}  -  ja estavam feitos",
                 font=("Consolas", 11), anchor="w").pack(anchor="w", padx=20, pady=2)
        cor = "#c62828" if resumo["atn"] > 0 else "#666"
        tk.Label(frame, text=f"ATENCAO    {resumo['atn']:>4}  -  precisam de acao manual",
                 font=("Consolas", 11), anchor="w", fg=cor).pack(anchor="w", padx=20, pady=2)

        frame_btn = tk.Frame(win, pady=15)
        frame_btn.pack()

        def abrir_path(p):
            try:
                os.startfile(p)
            except Exception as e:
                messagebox.showerror("Erro", f"Nao consegui abrir:\n{e}")

        if resumo.get("relatorio"):
            tk.Button(frame_btn, text="Relatorio",
                       command=lambda: abrir_path(resumo["relatorio"]),
                       padx=10, pady=5).pack(side=tk.LEFT, padx=5)
        if resumo.get("log"):
            tk.Button(frame_btn, text="Log",
                       command=lambda: abrir_path(resumo["log"]),
                       padx=10, pady=5).pack(side=tk.LEFT, padx=5)
        tk.Button(frame_btn, text="Saida (planilhas)",
                   command=lambda: abrir_path(str(SAIDA_DIR)),
                   padx=10, pady=5).pack(side=tk.LEFT, padx=5)
        tk.Button(frame_btn, text="Fechar", command=win.destroy,
                   padx=20, pady=5).pack(side=tk.LEFT, padx=5)

    def _reativar_controles(self):
        self.btn_iniciar.config(state=tk.NORMAL)
        self.btn_procurar.config(state=tk.NORMAL)
        self.btn_parar.config(state=tk.DISABLED)
        self.btn_atualizar.config(state=tk.NORMAL)
        self.entry_planilha.config(state=tk.NORMAL)

    # ---- Helpers de texto --------------------------------------------------
    def _append_log(self, linha: str):
        self.text_log.config(state=tk.NORMAL)
        self.text_log.insert(tk.END, linha)
        self.text_log.see(tk.END)
        self.text_log.config(state=tk.DISABLED)

    def _clear_log(self):
        self.text_log.config(state=tk.NORMAL)
        self.text_log.delete("1.0", tk.END)
        self.text_log.config(state=tk.DISABLED)


# ============================================================================
# ENTRYPOINT
# ============================================================================
def main():
    if not VENV_PYTHON.exists():
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Instalacao incompleta",
            f"Nao encontrei o Python do venv em:\n{VENV_PYTHON}\n\n"
            "Rode 'setup.ps1' primeiro pra configurar o robo:\n"
            "  powershell -ExecutionPolicy Bypass -File setup.ps1"
        )
        return 1

    root = tk.Tk()
    app = App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
