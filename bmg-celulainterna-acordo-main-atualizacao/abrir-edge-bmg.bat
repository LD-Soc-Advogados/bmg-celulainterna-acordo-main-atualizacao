@echo off
REM ============================================================
REM  abrir-edge-bmg.bat
REM  Abre o Microsoft Edge em modo "automacao remota" para o
REM  bmg-celulainterna-acordo se conectar via CDP (porta 9222).
REM
REM  IMPORTANTE: usa o MESMO perfil que o bmg-extrator e o
REM  bmg-workflow-closer (%USERPROFILE%\bmg-edge-profile). Assim
REM  o login no e-Xyon vale para os tres robos. Rode UM por vez.
REM ============================================================

set EDGE_EXE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe
if not exist "%EDGE_EXE%" set EDGE_EXE=C:\Program Files\Microsoft\Edge\Application\msedge.exe

if not exist "%EDGE_EXE%" (
    echo ERRO: nao encontrei o Microsoft Edge nos locais padrao.
    echo Procure msedge.exe no seu computador e ajuste o caminho neste .bat.
    pause
    exit /b 1
)

set PROFILE_DIR=%USERPROFILE%\bmg-edge-profile

echo ============================================================
echo  Abrindo Microsoft Edge para o bmg-celulainterna-acordo
echo  Perfil: %PROFILE_DIR%
echo  Porta de debug: 9222
echo ============================================================
echo.
echo  Faca o login no e-Xyon normalmente nesta janela do Edge.
echo  Depois rode o robo, por exemplo:
echo    .venv\Scripts\python.exe main.py
echo.

start "" "%EDGE_EXE%" --remote-debugging-port=9222 --user-data-dir="%PROFILE_DIR%" https://juridico.bancobmg.com.br/
