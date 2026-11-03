@echo off
REM MTransfer one-click launcher (Windows). Installs deps, runs the web wizard.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtualenv...
  python -m venv .venv
)
echo Installing dependencies...
.venv\Scripts\python -m pip install -q -r requirements.txt
echo.
echo ============================================================
echo   MTransfer is running:  http://127.0.0.1:8000
echo   Open this link in your browser. Ctrl+C to stop.
echo ============================================================
.venv\Scripts\python -m src.web
