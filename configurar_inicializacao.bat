@echo off
chcp 65001 > nul

echo ========================================================
echo   CONFIGURADOR DE INICIALIZAÇÃO AUTOMÁTICA (SEM ADMIN)
echo ========================================================
echo.

set FILE_NAME=IniciarConferencia.bat
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup

echo [1/2] Copiando script de inicialização automática para a pasta do usuário...
copy /y "%~dp0IniciarConferencia.bat" "%STARTUP_DIR%\%FILE_NAME%" >nul

if %errorLevel% eq 0 (
    echo [OK] Script copiado com sucesso para:
    echo %STARTUP_DIR%\%FILE_NAME%
    echo.
    echo [2/2] Iniciando o sistema em background pela primeira vez...
    start "" "%STARTUP_DIR%\%FILE_NAME%"
    echo.
    echo ========================================================
    echo ✅ CONFIGURAÇÃO CONCLUÍDA COM SUCESSO!
    echo.
    echo O sistema agora iniciará automaticamente sempre que o computador ligar.
    echo E já está rodando em segundo plano neste momento!
    echo Acesse: http://localhost:5000
    echo ========================================================
) else (
    echo [ERRO] Falha ao configurar a inicialização.
)

echo.
pause
