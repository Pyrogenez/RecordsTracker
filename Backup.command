#!/bin/bash
# Records Tracker - back up your data (macOS)
cd "$(dirname "$0")" || exit 1
pause() { echo; read -n 1 -s -r -p "Press any key to close this window..."; echo; }

if [ ! -f venv/bin/activate ]; then echo "  Please run Install.command first."; pause; exit 1; fi
# shellcheck disable=SC1091
source venv/bin/activate

echo
echo "=========================================================="
echo "  Backing up your data..."
echo "=========================================================="
echo
python -c "from records_tracker.backup import make_db_backup; m=make_db_backup('manual'); print('Backup saved:', m['name']) if m else print('No database yet - nothing to back up.')"
echo
echo "Backups are stored in the \"backups\" folder."
pause
