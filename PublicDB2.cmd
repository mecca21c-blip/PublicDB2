@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    powershell.exe -NoProfile -Command "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('Python 가상환경을 찾을 수 없습니다. 프로젝트 폴더에서 Python 환경을 준비한 뒤 다시 실행해 주세요.', 'PublicDB2 시작 오류') | Out-Null"
    exit /b 1
)

start "" /b ".venv\Scripts\pythonw.exe" "scripts\run_desktop.py"
exit /b 0
