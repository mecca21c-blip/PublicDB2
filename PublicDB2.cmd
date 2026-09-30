@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" goto missing_venv

start "" ".venv\Scripts\pythonw.exe" "scripts\run_desktop.py"
exit /b 0

:missing_venv
powershell.exe -NoProfile -Command "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('PublicDB2 virtual environment (.venv) was not found. Run setup first.', 'PublicDB2 startup error') | Out-Null"
exit /b 1
