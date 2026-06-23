@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo Please run "Install.bat" first.
    pause
    exit /b 1
)

call "venv\Scripts\activate.bat"

echo.
echo ==========================================================
echo   Pulling new records from the portal...
echo ==========================================================
echo.
echo This checks every request that is still OPEN and any new
echo requests that have appeared. Closed requests are skipped.
echo.

python run.py

echo.
echo Done.
pause
