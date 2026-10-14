@echo off
chcp 65001 >nul
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo Missing .venv. Follow README1.md installation steps first.
    goto finish
)
set "PYTHONPATH=%CD%\src"
set "PYTHONDONTWRITEBYTECODE=1"
".venv\Scripts\python.exe" -m companion %*
:finish
pause
