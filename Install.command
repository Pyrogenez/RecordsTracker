#!/bin/bash
# Records Tracker - First Time Setup (macOS)
cd "$(dirname "$0")" || exit 1

pause() { echo; read -n 1 -s -r -p "Press any key to close this window..."; echo; }

echo
echo "=========================================================="
echo "  Records Tracker - First Time Setup"
echo "=========================================================="
echo
echo "This will check Python, set up a private workspace, install"
echo "packages, download the scraping browser, and ask for your"
echo "portal login and API key. Takes about 5-10 minutes."
echo

# --- Step 1: Python 3.11+ ---
echo "[1/5] Checking for Python 3..."
if ! command -v python3 >/dev/null 2>&1; then
    echo "  Python 3 is NOT installed."
    echo "  Install Python 3.11 or newer from https://www.python.org/downloads/"
    echo "  then run Install.command again."
    open "https://www.python.org/downloads/" >/dev/null 2>&1
    pause; exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then
    echo "  Your Python is too old. This program needs Python 3.11 or newer."
    echo "  Install it from https://www.python.org/downloads/ and run again."
    open "https://www.python.org/downloads/" >/dev/null 2>&1
    pause; exit 1
fi
echo "  Found $(python3 --version)"

# --- Step 2: venv ---
echo
echo "[2/5] Creating private workspace (virtual environment)..."
if [ ! -f venv/bin/activate ]; then
    python3 -m venv venv || { echo "  ERROR: could not create the virtual environment."; pause; exit 1; }
    echo "  Workspace created."
else
    echo "  Workspace already exists, skipping."
fi
# shellcheck disable=SC1091
source venv/bin/activate

# --- Step 3: pip deps ---
echo
echo "[3/5] Installing required packages (this takes a minute)..."
python -m pip install --upgrade pip >/dev/null 2>&1
if ! python -m pip install -r requirements.txt; then
    echo "  ERROR: failed to install packages. Check your internet and try again."
    pause; exit 1
fi
echo "  Packages installed."

# --- Step 4: Playwright browser ---
echo
echo "[4/5] Downloading the browser used for scraping (can take 5+ minutes)..."
if ! python -m playwright install chromium; then
    echo "  ERROR: browser download failed."
    pause; exit 1
fi
echo "  Browser installed."

# --- Step 5: Wizard ---
echo
echo "[5/5] Configuration..."
python setup_wizard.py || { echo "  Setup cancelled. Run Install.command again to finish."; pause; exit 1; }

echo
echo "=========================================================="
echo "  All done!"
echo "=========================================================="
echo "  - Double-click Start.command to open the web interface."
echo "  - First time? Run FullScrape.command once to pull everything."
echo
pause
