#!/bin/bash
# Records Tracker - restore your data from a backup (macOS)
cd "$(dirname "$0")" || exit 1
pause() { echo; read -n 1 -s -r -p "Press any key to close this window..."; echo; }

if [ ! -f venv/bin/activate ]; then echo "  Please run Install.command first."; pause; exit 1; fi
# shellcheck disable=SC1091
source venv/bin/activate

echo
echo "=========================================================="
echo "  Restore your data from a backup"
echo "=========================================================="
echo
echo "  IMPORTANT: close the Records Tracker window (Start) before restoring."
echo "  Your current data is backed up first, so a restore can be undone."
echo
python restore_data.py
echo
read -r -p "Enter the NUMBER of the backup to restore (or press Enter to cancel): " CHOICE
if [ -n "$CHOICE" ]; then python restore_data.py "$CHOICE"; fi
pause
