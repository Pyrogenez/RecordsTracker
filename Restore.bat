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
echo   Restore your data from a backup
echo ==========================================================
echo.
echo   IMPORTANT: close the Records Tracker window (Start) before restoring.
echo   Your current data is backed up first, so a restore can be undone.
echo.

python restore_data.py
echo.
set /p CHOICE="Enter the NUMBER of the backup to restore (or just close this window to cancel): "
if not "%CHOICE%"=="" python restore_data.py %CHOICE%
echo.
pause
