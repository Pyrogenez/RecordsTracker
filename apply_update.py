"""Apply a RecordsTracker update zip safely (invoked by Update.bat).

Why a Python script instead of batch/PowerShell: it can stage, validate, back
up, copy, and prune deterministically — and it can be tested. It uses ONLY the
standard library so it never depends on the venv (which Update.bat refreshes
afterwards). The user's data and secrets are never touched.

Exit codes:  0 success · 2 no zip · 3 unreadable zip · 4 wrong layout · 5 copy error
"""
from __future__ import annotations

import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Never overwritten, backed up, or shipped in an update.
PRESERVE = {"credentials.json", "config.json"}
# Top-level names skipped when backing up / copying (user data, envs, scratch).
SKIP_TOP = {
    "data", "logs", "venv", ".venv", "__pycache__",
    ".update_staging", ".update_backup", ".applied_updates", ".git",
} | PRESERVE
KEEP_BACKUPS = 3


def find_zip() -> Path | None:
    candidates = sorted(ROOT.glob("update-*.zip")) + list(ROOT.glob("update.zip"))
    return candidates[0] if candidates else None


def looks_like_release(d: Path) -> bool:
    return (d / "VERSION.txt").exists() and (d / "server.py").exists()


def resolve_source(staging: Path) -> Path | None:
    """The update contents — handle both a flat zip and one that wrapped a
    single top-level folder (the common 'Send to > Compressed folder' shape)."""
    if looks_like_release(staging):
        return staging
    subdirs = [p for p in staging.iterdir() if p.is_dir()]
    wrapped = [p for p in subdirs if looks_like_release(p)]
    return wrapped[0] if len(wrapped) == 1 else None


def safe_removed_entry(rel: str) -> bool:
    rel = rel.strip()
    if not rel or rel.startswith("#"):
        return False
    norm = rel.replace("\\", "/")
    if norm.startswith("/") or ":" in norm:
        return False
    parts = Path(norm).parts
    if ".." in parts:
        return False
    return parts[0] not in SKIP_TOP


def prune_backups() -> None:
    base = ROOT / ".update_backup"
    if not base.exists():
        return
    snaps = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name)
    for old in snaps[:-KEEP_BACKUPS]:
        shutil.rmtree(old, ignore_errors=True)


def main() -> int:
    zp = find_zip()
    if not zp:
        print("ERROR: no update-*.zip found in this folder.")
        return 2

    staging = ROOT / ".update_staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zp) as z:
            z.extractall(staging)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: could not read {zp.name}: {e}")
        shutil.rmtree(staging, ignore_errors=True)
        return 3

    src = resolve_source(staging)
    if src is None:
        print(
            "ERROR: this zip does not look like a RecordsTracker release\n"
            "       (no VERSION.txt + server.py at its top level).\n"
            "       Ask the sender to zip the CONTENTS of the folder, not the\n"
            "       folder itself. Nothing on your computer was changed."
        )
        shutil.rmtree(staging, ignore_errors=True)
        return 4

    # 1) Back up current code (excluding data/secrets) so a bad update is reversible.
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = ROOT / ".update_backup" / ts
    backup.mkdir(parents=True, exist_ok=True)
    try:
        for item in ROOT.iterdir():
            if item.name in SKIP_TOP:
                continue
            dest = backup / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

        # 2) Copy staged code over the live install (never the preserved files).
        copied = 0
        for item in sorted(src.rglob("*")):
            rel = item.relative_to(src)
            if rel.parts and rel.parts[0] in PRESERVE:
                continue
            target = ROOT / rel
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
                copied += 1
    except Exception as e:  # noqa: BLE001
        print(f"ERROR while applying files: {e}\n"
              f"       A backup of your previous code is in {backup}")
        shutil.rmtree(staging, ignore_errors=True)
        return 5

    # 3) Remove files a new version deleted/renamed (guarded manifest, optional).
    removed = 0
    manifest = src / "removed_files.txt"
    if manifest.exists():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if not safe_removed_entry(line):
                if line.strip() and not line.strip().startswith("#"):
                    print(f"  skipped unsafe removed-file entry: {line.strip()!r}")
                continue
            tgt = ROOT / line.strip()
            try:
                if tgt.is_file():
                    tgt.unlink()
                    removed += 1
            except OSError:
                pass

    shutil.rmtree(staging, ignore_errors=True)
    prune_backups()
    ver = "?"
    try:
        ver = (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    print(f"Applied {zp.name}: {copied} file(s) updated, {removed} removed. "
          f"Now at v{ver}. (Previous code backed up in .update_backup/{ts}.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
