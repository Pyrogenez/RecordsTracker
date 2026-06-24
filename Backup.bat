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
echo   Backing up your data...
echo ==========================================================
echo.
python -c "from records_tracker.backup import make_db_backup; m=make_db_backup('manual'); print('Backup saved:', m['name']) if m else print('No database yet - nothing to back up.')"
echo.
echo Backups are stored in the "backups" folder.
echo.
pause
