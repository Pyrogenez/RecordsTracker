@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ==========================================================
echo   Records Tracker - Update
echo ==========================================================
echo.

REM -- Find an update zip in the current folder --
set UPDATE_ZIP=
for %%f in (update-*.zip) do (
    set UPDATE_ZIP=%%f
    goto :found
)
for %%f in (update.zip) do (
    set UPDATE_ZIP=%%f
    goto :found
)

echo   No update file found in this folder.
echo.
echo   To update:
echo     1. Save the update file (e.g. "update-1.1.0.zip")
echo        into this folder (the same folder as Update.bat).
echo     2. Run Update.bat again.
echo.
pause
exit /b 1

:found
echo   Found update file: !UPDATE_ZIP!
echo.

REM -- Back up the user's login + settings as a precaution --
if not exist ".update_backup" mkdir .update_backup
if exist credentials.json copy /y credentials.json .update_backup\ >nul
if exist config.json      copy /y config.json      .update_backup\ >nul

REM -- Extract the update zip on top of the current folder --
echo   Applying update...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "Expand-Archive -Path '!UPDATE_ZIP!' -DestinationPath '.' -Force"
if errorlevel 1 (
    echo   ERROR: failed to extract !UPDATE_ZIP!
    pause
    exit /b 1
)

REM -- Defense in depth: if the update zip somehow included a
REM    credentials.json or config.json, put the user's own back.
if exist .update_backup\credentials.json copy /y .update_backup\credentials.json credentials.json >nul
if exist .update_backup\config.json      copy /y .update_backup\config.json      config.json      >nul

REM -- Refresh Python packages in case requirements.txt changed --
if exist "venv\Scripts\activate.bat" (
    echo.
    echo   Refreshing Python packages...
    call "venv\Scripts\activate.bat"
    python -m pip install -r requirements.txt
)

REM -- Move the applied update aside so it doesn't run again --
if not exist ".applied_updates" mkdir .applied_updates
move /y "!UPDATE_ZIP!" ".applied_updates\" >nul

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
echo   Your login, settings, database, and downloaded files
echo   were NOT touched.
echo.
pause
