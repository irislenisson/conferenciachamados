@echo off
chcp 65001 > nul

echo ========================================================
echo   FINALIZANDO SISTEMA DE CONFERÊNCIA (PORTA 5000)
echo ========================================================
echo.

:: Localiza o PID do processo que escuta na porta 5000
set PID=
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :5000 ^| findstr LISTENING') do set PID=%%a

if "%PID%"=="" (
    echo [INFO] Nenhuma instância do sistema rodando na porta 5000 foi encontrada.
    echo.
    pause
    exit /b 0
)

echo [1/2] Parando o processo PID %PID% associado à porta 5000...
taskkill /F /PID %PID%
if %errorLevel% eq 0 (
    echo [2/2] Sistema encerrado com sucesso!
) else (
    echo [ERRO] Falha ao encerrar o processo.
)

echo.
pause
