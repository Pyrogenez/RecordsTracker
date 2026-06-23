@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo Please run "Install.bat" first.
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
echo   FULL scrape - pulls every request, even closed ones
echo ==========================================================
echo.
echo Use this:
echo   - The FIRST time you run the program.
echo   - If you want to refresh everything from scratch.
echo.
echo This can take a long time (many minutes to an hour).
echo Your attachments will be downloaded as it goes.
echo.
choice /c YN /m "Continue with a full scrape"
if errorlevel 2 (
    echo Cancelled.
    pause
    exit /b 0
)

python run.py --full

echo.
echo Done.
pause
