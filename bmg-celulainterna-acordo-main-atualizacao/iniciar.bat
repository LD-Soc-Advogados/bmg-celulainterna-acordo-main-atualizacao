@echo off
REM Abre a GUI do robo bmg-celulainterna-acordo.
REM Duplo-clique aqui pra usar.

setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo.
    echo ERRO: ambiente virtual nao encontrado em .venv\
    echo.
    echo Rode setup.ps1 primeiro pra configurar o robo:
    echo     powershell -ExecutionPolicy Bypass -File setup.ps1
    echo.
    pause
    exit /b 1
)

REM pythonw.exe roda sem abrir janela de console (a GUI tkinter ja tem janela propria)
start "" ".venv\Scripts\pythonw.exe" "iniciar.py"
endlocal
