@echo off
cd /d "%~dp0"
start /B "" ".venv\Scripts\python.exe" app.py > server.log 2>&1
