@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ==========================================================
echo   Records Tracker - Update
echo ==========================================================
echo.

REM Pick a Python: prefer the program's own venv, fall back to system Python.
set "PY=python"
if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"

REM Stage, validate, back up, and apply the update zip (this never touches your
REM data, downloads, login, or settings). apply_update.py reports a clear error
REM and changes nothing if the zip is missing or has the wrong layout.
"%PY%" apply_update.py
if errorlevel 1 (
    echo.
    echo   Update was NOT applied. Nothing on your computer was changed.
    echo.
    pause
    exit /b 1
)

REM Refresh Python packages in case requirements.txt changed. Abort (without
REM archiving the zip) if it fails, so you can fix your connection and re-run.
if exist "venv\Scripts\activate.bat" (
    echo.
    echo   Refreshing Python packages...
    call "venv\Scripts\activate.bat"
    python -m pip install -r requirements.txt
)
if errorlevel 1 (
    echo.
    echo   ERROR: refreshing packages failed. Your code was updated, but the
    echo   dependencies may be out of date. Check your internet connection and
    echo   run Update.bat again - it is safe to re-run.
    echo.
    pause
    exit /b 1
)

REM Move the applied zip aside so it does not run again.
if not exist ".applied_updates" mkdir ".applied_updates"
for %%f in (update-*.zip update.zip) do (
    if exist "%%f" move /y "%%f" ".applied_updates\" >nul
)

echo.
echo ==========================================================
echo   Update complete
echo ==========================================================
echo.
if exist VERSION.txt (
    echo   You are now running version:
    type VERSION.txt
    echo.
)
echo   Your login, settings, database, and downloaded files were
echo   NOT touched. A backup of the previous code is in .update_backup.
echo.
pause
