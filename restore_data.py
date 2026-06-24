"""List and restore data backups (used by Restore.bat / Restore.command).

  python restore_data.py            - list available backups
  python restore_data.py <name|#>   - restore that backup (asks for confirmation)

Each backup is a consistent snapshot of your database taken automatically before
every update (and whenever you click "Back up now"). Restoring REPLACES your
current data with the snapshot — but your current data is itself backed up first,
so a restore is always reversible. Close the program before restoring.
"""
from __future__ import annotations

import sys

from records_tracker.backup import list_backups, restore_db_backup


def _fmt(b: dict) -> str:
    kb = (b.get("db_bytes") or 0) // 1024
    integ = "" if b.get("integrity") in ("ok", "?", None) else f"  [integrity: {b['integrity']}]"
    return (f"{b['name']}   v{b.get('version', '?')}   {kb} KB   "
            f"({b.get('reason', '?')}){integ}")


def main() -> int:
    backups = list_backups()
    if not backups:
        print("No backups found yet. One is created automatically before each "
              "update, or click 'Back up now' in the web UI.")
        return 0

    args = sys.argv[1:]
    if not args:
        print("Available backups (newest first):\n")
        for i, b in enumerate(backups, 1):
            print(f"  [{i}]  {_fmt(b)}")
        print("\nTo restore one, run:   python restore_data.py <number or name>")
        print("Close the program (Start window) first. Your current data is backed "
              "up automatically before a restore, so it's reversible.")
        return 0

    sel = args[0]
    name = sel
    if sel.isdigit():
        idx = int(sel) - 1
        if not (0 <= idx < len(backups)):
            print(f"No backup #{sel}. Run with no arguments to list them.", file=sys.stderr)
            return 1
        name = backups[idx]["name"]
    elif sel not in {b["name"] for b in backups}:
        print(f"No backup named {sel!r}. Run with no arguments to list them.", file=sys.stderr)
        return 1

    print(f"\nThis will REPLACE your current data with backup:\n    {name}")
    print("Your current data is backed up first, so this can be undone.")
    try:
        resp = input("Type YES to proceed: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 1
    if resp != "YES":
        print("Cancelled.")
        return 0
    try:
        res = restore_db_backup(name)
    except Exception as e:  # noqa: BLE001
        print(f"\nDid NOT restore: {e}")
        return 1
    print(f"\nRestored from {res['restored']}.")
    if res.get("safety_backup"):
        print(f"Your previous data was saved as: {res['safety_backup']}")
    print("Start the program again to use the restored data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
