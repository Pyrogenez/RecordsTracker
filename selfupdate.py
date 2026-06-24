"""Self-update from GitHub.

  python selfupdate.py check   - report whether a newer version is available
  python selfupdate.py apply   - download the latest version and apply it
                                 (your data is backed up automatically first)

Used by the web UI's "Update now" button and by CheckUpdate.bat / .command.
Applying an update never touches your data, login, or settings — only code —
and a snapshot of your database is taken before anything changes.
"""
from __future__ import annotations

import sys

import apply_update
from records_tracker import updater


def cmd_check() -> int:
    info = updater.check(force=True)
    current = info["current"]
    if not info.get("enabled"):
        print("Automatic update checking is turned off in config.json.")
        return 0
    latest = info.get("latest")
    if latest is None:
        print(f"Couldn't reach GitHub to check for updates. You're on v{current}.")
        return 0
    if info.get("available"):
        print(f"An update is available: v{current}  ->  v{latest}")
        print("Run:  python selfupdate.py apply   (or use 'Update now' in the web UI)")
        return 10
    print(f"You're up to date (v{current}).")
    return 0


def cmd_apply() -> int:
    info = updater.check(force=True)
    if not info.get("available"):
        print(f"Nothing to apply (current v{info['current']}, "
              f"latest {info.get('latest')}).")
        return 0
    latest = info["latest"]
    print(f"Downloading version {latest} from GitHub...")
    try:
        updater.download_update(latest)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: could not download the update: {e}", file=sys.stderr)
        return 1
    print("Applying update (your data is backed up first, code only is replaced)...")
    # apply_update finds the downloaded update-*.zip, snapshots the database,
    # backs up the current code, then applies (auto-rolling back on failure).
    rc = apply_update.main()
    if rc != 0:
        return rc
    # Refresh dependencies in case requirements.txt changed — otherwise a version
    # that adds a package would fail to start. The server runs under the venv, so
    # sys.executable is the right interpreter.
    print("Refreshing dependencies...")
    try:
        import subprocess
        pip = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
            capture_output=True, text=True)
        if pip.returncode != 0:
            print("WARNING: dependency refresh failed. If the app won't start, "
                  "double-click Update.bat / Update.command (safe to re-run) to finish.")
            print((pip.stderr or "")[-800:])
            return 7
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: could not refresh dependencies ({e}). If the app won't "
              "start, run Update.bat / Update.command to finish.")
        return 7
    print("Done. Close the program and reopen with Start to use the new version.")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        return cmd_check()
    if cmd == "apply":
        return cmd_apply()
    print("Usage: python selfupdate.py [check|apply]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
