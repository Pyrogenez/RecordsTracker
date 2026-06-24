#!/bin/bash
# Records Tracker - apply an update zip (macOS)
cd "$(dirname "$0")" || exit 1
pause() { echo; read -n 1 -s -r -p "Press any key to close this window..."; echo; }

echo
echo "=========================================================="
echo "  Records Tracker - Update"
echo "=========================================================="
echo

# Prefer the program's own venv Python; fall back to system python3.
PY="python3"
[ -x venv/bin/python ] && PY="venv/bin/python"

# Stage, validate, back up, and apply (never touches your data/login/settings).
if ! "$PY" apply_update.py; then
    echo
    echo "  Update was NOT applied. Nothing on your computer was changed."
    pause; exit 1
fi

# Refresh packages; abort (without archiving the zip) if it fails so re-runs work.
if [ -f venv/bin/activate ]; then
    echo
    echo "  Refreshing Python packages..."
    # shellcheck disable=SC1091
    source venv/bin/activate
    if ! python -m pip install -r requirements.txt; then
        echo "  ERROR: refreshing packages failed. Fix your internet and re-run Update.command."
        pause; exit 1
    fi
fi

# Move the applied zip aside so it doesn't run again.
mkdir -p .applied_updates
for f in update-*.zip update.zip; do
    [ -f "$f" ] && mv -f "$f" .applied_updates/
done

echo
echo "=========================================================="
echo "  Update complete"
echo "=========================================================="
if [ -f VERSION.txt ]; then echo "  Now running version:"; cat VERSION.txt; fi
echo "  Your login, settings, database, and downloaded files were NOT touched."
echo "  A backup of the previous code is in .update_backup."
echo
pause
