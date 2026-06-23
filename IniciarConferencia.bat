@echo off
:: Aguarda 5 segundos para que o Windows monte o perfil de usuário e a rede completamente
timeout /t 5 /nobreak >nul

cd /d "c:\Users\irislenisson.souza\.gemini\antigravity\scratch\sistema_conferencia"
start "" ".venv\Scripts\pythonw.exe" app.py
