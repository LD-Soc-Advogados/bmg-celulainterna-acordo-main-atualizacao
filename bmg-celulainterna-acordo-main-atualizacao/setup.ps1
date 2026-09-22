# Setup do bmg-celulainterna-acordo num PC do zero.
#
# Rode na raiz do projeto (depois de clonar):
#     powershell -ExecutionPolicy Bypass -File setup.ps1
#
# Faz tudo: cria venv, instala dependencias, instala Playwright/Edge driver,
# cria .env (se nao existir), cria pastas saida/ e downloads/.
#
# Verifica pre-requisitos: Python 3.10+ e Microsoft Edge. Se faltar, instrui
# o usuario a instalar antes (nao tenta instalar automaticamente pra evitar
# erros silenciosos com permissoes de admin).

$ErrorActionPreference = "Stop"
$ROOT = $PSScriptRoot

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  SETUP - bmg-celulainterna-acordo" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# ---- 1) Verifica Python 3.10+ ----
Write-Host "[1/6] Verificando Python..." -ForegroundColor Yellow
try {
    $pyVersion = & python --version 2>&1
    Write-Host "       OK: $pyVersion"
} catch {
    Write-Host "       ERRO: Python nao encontrado no PATH." -ForegroundColor Red
    Write-Host ""
    Write-Host "       Instale Python 3.10+ em https://www.python.org/downloads/"
    Write-Host "       MARQUE 'Add Python to PATH' no instalador."
    Write-Host ""
    exit 1
}

# Confere versao minima 3.10
$pyMajorMinor = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$pyParts = $pyMajorMinor.Split(".")
if ([int]$pyParts[0] -lt 3 -or ([int]$pyParts[0] -eq 3 -and [int]$pyParts[1] -lt 10)) {
    Write-Host "       ERRO: Python $pyMajorMinor instalado, precisa de 3.10+." -ForegroundColor Red
    exit 1
}

# ---- 2) Verifica Edge ----
Write-Host "[2/6] Verificando Microsoft Edge..." -ForegroundColor Yellow
$edgePath = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
$edgePathAlt = "${env:ProgramFiles}\Microsoft\Edge\Application\msedge.exe"
if (Test-Path $edgePath) {
    Write-Host "       OK: $edgePath"
} elseif (Test-Path $edgePathAlt) {
    Write-Host "       OK: $edgePathAlt"
} else {
    Write-Host "       AVISO: Edge nao encontrado nos caminhos padrao." -ForegroundColor Yellow
    Write-Host "       Se o Edge esta instalado em outro lugar, o robo pode falhar."
    Write-Host "       Continuando mesmo assim..."
}

# ---- 3) Cria venv ----
Write-Host "[3/6] Criando ambiente virtual (.venv)..." -ForegroundColor Yellow
$venvPath = Join-Path $ROOT ".venv"
if (Test-Path $venvPath) {
    Write-Host "       .venv ja existe - pulando criacao."
} else {
    & python -m venv $venvPath
    Write-Host "       OK: .venv criado em $venvPath"
}

$venvPython = Join-Path $venvPath "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "       ERRO: $venvPython nao existe apos criacao do venv." -ForegroundColor Red
    exit 1
}

# ---- 4) Instala requirements ----
Write-Host "[4/6] Instalando dependencias (pode demorar 1-2 min)..." -ForegroundColor Yellow
$req = Join-Path $ROOT "requirements.txt"
if (-not (Test-Path $req)) {
    Write-Host "       ERRO: requirements.txt nao encontrado." -ForegroundColor Red
    exit 1
}
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r $req
Write-Host "       OK"

# ---- 5) Instala browsers do Playwright (msedge + chromium) ----
# msedge: usado pelas tarefas do e-Xyon (via CDP no Edge dedicado).
# chromium: navegador LIMPO usado pela Tarefa 5 (CNA) - perfil neutro,
#           cai menos no captcha do que o Edge dedicado cheio de automacao.
Write-Host "[5/6] Instalando navegadores do Playwright (Edge + Chromium)..." -ForegroundColor Yellow
& $venvPython -m playwright install msedge
& $venvPython -m playwright install chromium
Write-Host "       OK"

# ---- 6) Cria .env (se nao existir) + pastas ----
Write-Host "[6/6] Criando .env e pastas..." -ForegroundColor Yellow
$envFile = Join-Path $ROOT ".env"
$envExample = Join-Path $ROOT ".env.example"
if (-not (Test-Path $envFile)) {
    if (Test-Path $envExample) {
        Copy-Item $envExample $envFile
        Write-Host "       OK: .env criado a partir de .env.example"
        Write-Host "       AVISO: edite .env e ajuste DOWNLOADS_DIR pro seu drive." -ForegroundColor Yellow
    } else {
        Write-Host "       AVISO: .env.example nao encontrado - .env nao foi criado." -ForegroundColor Yellow
    }
} else {
    Write-Host "       .env ja existe - mantendo o atual."
}

foreach ($pasta in @("saida", "downloads", "logs")) {
    $p = Join-Path $ROOT $pasta
    if (-not (Test-Path $p)) {
        New-Item -ItemType Directory -Path $p | Out-Null
        Write-Host "       OK: pasta $pasta\ criada."
    }
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  SETUP CONCLUIDO" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Proximos passos:"
Write-Host "  1. Edite .env se quiser ajustar DOWNLOADS_DIR."
Write-Host "  2. Coloque uma 'Planilha Envio de casos*.xlsx' na raiz."
Write-Host "  3. Duplo-clique em iniciar.bat."
Write-Host ""
