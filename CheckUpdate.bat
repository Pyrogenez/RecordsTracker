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
echo   Checking for updates...
echo ==========================================================
echo.
python selfupdate.py check
if errorlevel 10 (
    echo.
    choice /c YN /m "Download and apply this update now (your data is backed up first)"
    if errorlevel 2 (
        echo Skipped. You can update later from here or the web interface.
    ) else (
        python selfupdate.py apply
    )
)
echo.
pause
