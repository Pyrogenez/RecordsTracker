@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo.
    echo   This program has not been set up yet.
    echo   Please run "Install.bat" first.
    echo.
    pause
    exit /b 1
)

if not exist "credentials.json" (
    echo.
    echo   Your portal login has not been configured yet.
    echo   Please run "Install.bat" to finish setup.
    echo.
    pause
    exit /b 1
)

call "venv\Scripts\activate.bat"

echo.
echo ==========================================================
echo   Records Tracker is starting...
echo ==========================================================
echo.
echo The web interface will open in your browser in a moment.
echo.
echo KEEP THIS WINDOW OPEN while you're using the program.
echo To stop the program, close this window or press Ctrl+C.
echo.

python server.py --open
