@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" goto novenv

start "" ".venv\Scripts\pythonw.exe" "%~dp0app.py"
exit /b 0

:novenv
echo.
echo   Virtual environment not found in .venv
echo   Please run setup.bat first to install dependencies.
echo.
pause
exit /b 1
