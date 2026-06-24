#!/bin/bash
# Records Tracker - quick incremental scrape (macOS)
cd "$(dirname "$0")" || exit 1
pause() { echo; read -n 1 -s -r -p "Press any key to close this window..."; echo; }

if [ ! -f venv/bin/activate ]; then echo "  Please run Install.command first."; pause; exit 1; fi
if [ ! -f credentials.json ]; then echo "  Please run Install.command to finish setup."; pause; exit 1; fi
# shellcheck disable=SC1091
source venv/bin/activate

echo
echo "=========================================================="
echo "  Pulling new records from the portal..."
echo "=========================================================="
echo "  Checks every OPEN request plus any new ones. Closed are skipped."
echo
python run.py
echo
echo "Done."
pause
