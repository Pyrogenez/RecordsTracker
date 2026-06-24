#!/bin/bash
# Records Tracker - launch the web interface (macOS)
cd "$(dirname "$0")" || exit 1
pause() { echo; read -n 1 -s -r -p "Press any key to close this window..."; echo; }

if [ ! -f venv/bin/activate ]; then
    echo "  This program has not been set up yet. Please run Install.command first."
    pause; exit 1
fi
if [ ! -f credentials.json ]; then
    echo "  Your portal login isn't configured yet. Please run Install.command."
    pause; exit 1
fi
# shellcheck disable=SC1091
source venv/bin/activate

echo
echo "=========================================================="
echo "  Records Tracker is starting..."
echo "=========================================================="
echo "  The web interface will open in your browser in a moment."
echo "  KEEP THIS WINDOW OPEN while using the program."
echo "  To stop, close this window or press Ctrl+C."
echo
python server.py --open
