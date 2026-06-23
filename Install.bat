@echo off
setlocal
cd /d "%~dp0"

echo.
echo ==========================================================
echo   Records Tracker - First Time Setup
echo ==========================================================
echo.
echo This will:
echo   1. Check that Python is installed
echo   2. Set up a private workspace for this program
echo   3. Download the packages it needs
echo   4. Download the built-in browser used for scraping
echo   5. Ask for your portal login and API key
echo.
echo The whole thing takes about 5-10 minutes, depending on
echo your internet speed. Do not close this window.
echo.
pause

REM --- Step 1: Python ---
echo.
echo [1/5] Checking for Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Python is NOT installed on this computer.
    echo.
    echo   Please:
    echo     1. Go to https://www.python.org/downloads/
    echo     2. Download Python 3.11 or newer.
    echo     3. Run the installer.
    echo     4. IMPORTANT: check the box "Add python.exe to PATH"
    echo        on the very first installer screen.
    echo     5. Come back and run Install.bat again.
    echo.
    start https://www.python.org/downloads/
    pause
    exit /b 1
)
for /f "tokens=* usebackq" %%v in (`python --version`) do set PYVER=%%v
echo   Found %PYVER%

REM --- Step 2: venv ---
echo.
echo [2/5] Creating private workspace (virtual environment)...
if not exist "venv\Scripts\activate.bat" (
    python -m venv venv
    if errorlevel 1 (
        echo   ERROR: failed to create virtual environment.
        pause
        exit /b 1
    )
    echo   Workspace created.
) else (
    echo   Workspace already exists, skipping.
)

call "venv\Scripts\activate.bat"

REM --- Step 3: pip deps ---
echo.
echo [3/5] Installing required packages (this takes a minute)...
python -m pip install --upgrade pip >nul 2>&1
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo   ERROR: failed to install packages.
    echo   Check your internet connection and try again.
    pause
    exit /b 1
)
echo   Packages installed.

REM --- Step 4: Playwright browser ---
echo.
echo [4/5] Downloading the browser used for scraping...
echo   This can take 5+ minutes on a slow connection. Be patient.
python -m playwright install chromium
if errorlevel 1 (
    echo   ERROR: Playwright browser download failed.
    pause
    exit /b 1
)
echo   Browser installed.

REM --- Step 5: Wizard ---
echo.
echo [5/5] Configuration...
python setup_wizard.py
if errorlevel 1 (
    echo.
    echo   Setup was cancelled. You can run Install.bat again later
    echo   to finish configuration.
    pause
    exit /b 1
)

echo.
echo ==========================================================
echo   All done!
echo ==========================================================
echo.
echo You can now:
echo   - Double-click "Start.bat" to open the web interface.
echo   - Double-click "Scrape.bat" to pull new records from the portal.
echo.
echo First time? Run "FullScrape.bat" once to pull everything you have.
echo After that, "Scrape.bat" is faster (it only checks open records).
echo.
pause
