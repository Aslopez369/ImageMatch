@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 goto nopython

echo [1/2] Creating virtual environment in .venv ...
python -m venv .venv
if errorlevel 1 goto fail

echo [2/2] Installing dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail

echo.
echo   Done. Double-click run.bat to start.
echo.
pause
exit /b 0

:nopython
echo.
echo   Python not found on PATH.
echo   Install Python 3.9+ from https://www.python.org/downloads/
echo   During installation, tick both:
echo     - Add python.exe to PATH
echo     - tcl/tk and IDLE   (needed by the GUI)
echo.
pause
exit /b 1

:fail
echo.
echo   Setup failed. See the messages above.
echo.
pause
exit /b 1
