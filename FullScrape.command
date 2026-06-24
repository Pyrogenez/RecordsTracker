#!/bin/bash
# Records Tracker - full scrape, every request (macOS)
cd "$(dirname "$0")" || exit 1
pause() { echo; read -n 1 -s -r -p "Press any key to close this window..."; echo; }

if [ ! -f venv/bin/activate ]; then echo "  Please run Install.command first."; pause; exit 1; fi
if [ ! -f credentials.json ]; then echo "  Please run Install.command to finish setup."; pause; exit 1; fi
# shellcheck disable=SC1091
source venv/bin/activate

echo
echo "=========================================================="
echo "  FULL scrape - pulls every request, even closed ones"
echo "=========================================================="
echo "  Use this the first time, or to refresh everything."
echo "  This can take a long time (many minutes to an hour)."
echo
read -r -p "Continue with a full scrape? [y/N] " ans
case "$ans" in
    [Yy]*) ;;
    *) echo "Cancelled."; pause; exit 0 ;;
esac
python run.py --full
echo
echo "Done."
pause
