#!/bin/bash
# Records Tracker - check for and apply updates (macOS)
cd "$(dirname "$0")" || exit 1
pause() { echo; read -n 1 -s -r -p "Press any key to close this window..."; echo; }

if [ ! -f venv/bin/activate ]; then echo "  Please run Install.command first."; pause; exit 1; fi
# shellcheck disable=SC1091
source venv/bin/activate

echo
echo "=========================================================="
echo "  Checking for updates..."
echo "=========================================================="
echo
python selfupdate.py check
status=$?
if [ "$status" -eq 10 ]; then
    echo
    read -r -p "Download and apply this update now (your data is backed up first)? [y/N] " ans
    case "$ans" in
        [Yy]*) python selfupdate.py apply ;;
        *) echo "Skipped. You can update later from here or the web interface." ;;
    esac
fi
pause
