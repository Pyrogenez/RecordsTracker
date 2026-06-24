"""Apply a RecordsTracker update zip safely (invoked by Update.bat).

Why a Python script instead of batch/PowerShell: it can stage, validate, back
up, copy, and prune deterministically — and it can be tested. It uses ONLY the
standard library so it never depends on the venv (which Update.bat refreshes
afterwards). The user's data and secrets are never touched.

Exit codes:  0 success · 2 no zip · 3 unreadable zip · 4 wrong layout ·
             5 copy error (auto-rolled back) · 6 could not back up data (nothing changed)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Never overwritten, backed up, or shipped in an update.
PRESERVE = {"credentials.json", "config.json"}
# Top-level names skipped when backing up / copying (user data, envs, scratch,
# and the data backups themselves — an update must NEVER touch any of these).
SKIP_TOP = {
    "data", "logs", "venv", ".venv", "__pycache__", "backups",
    ".update_staging", ".update_backup", ".applied_updates", ".git",
} | PRESERVE
KEEP_BACKUPS = 3
KEEP_DB_BACKUPS = 10


def _ver_key(p: Path) -> list:
    m = re.search(r"update-(.+)$", p.stem)
    s = m.group(1) if m else "0"
    out = []
    for tok in s.split("."):
        digits = "".join(c for c in tok if c.isdigit())
        out.append(int(digits) if digits else 0)
    return out


def find_zip() -> Path | None:
    # Pick the HIGHEST-version update zip, not the lexicographically-first one
    # (so update-1.10.0 beats update-1.2.0 and we never apply a stale/older zip).
    versioned = list(ROOT.glob("update-*.zip"))
    if versioned:
        versioned.sort(key=_ver_key)
        return versioned[-1]
    plain = list(ROOT.glob("update.zip"))
    return plain[0] if plain else None


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


def _objects(db: Path) -> int:
    con = sqlite3.connect(str(db))
    try:
        return con.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
    finally:
        con.close()


def _complete_db_backups() -> list[Path]:
    base = ROOT / "backups"
    if not base.exists():
        return []
    out = []
    for d in base.iterdir():
        if not d.is_dir() or d.name.startswith(".staging-"):
            continue
        db, meta = d / "records.db", d / "meta.json"
        try:
            if db.exists() and meta.exists() and db.stat().st_size > 0:
                out.append(d)
        except OSError:
            pass
    return sorted(out, key=lambda d: d.name)


def _prune_db_backups() -> None:
    """Integrity/role-aware prune mirroring records_tracker/backup.py: never drop
    the most recent good backup nor the most recent pre-restore safety copy."""
    items = _complete_db_backups()  # oldest -> newest
    if len(items) <= KEEP_DB_BACKUPS:
        return
    def meta(d):
        try:
            return json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            return {}
    keep = {d.name for d in items[-KEEP_DB_BACKUPS:]}
    for d in reversed(items):  # newest first
        m = meta(d)
        if m.get("integrity") == "ok":
            keep.add(d.name); break
    for d in reversed(items):
        if m and meta(d).get("reason") == "pre-restore":
            keep.add(d.name); break
    for d in items:
        if d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)


def backup_database() -> str | None:
    """Snapshot data/records.db into backups/<ts>__pre-update/ BEFORE applying an
    update, so the user can revert their data if a future version ever misbehaves.
    Built in a temp dir and only committed (atomic rename) once the copy succeeded
    and actually contains the data — a half-written/empty snapshot is never kept.

    Returns the backup name on success, None if there is no database yet, and
    RAISES on an engine failure (disk full / permission) so the caller can stop
    the update rather than apply one with no revert point. (An already-corrupt DB
    is still snapshotted, marked integrity != 'ok' — that's the user's data.)
    """
    db = ROOT / "data" / "records.db"
    if not db.exists():
        return None
    src_objs = _objects(db)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = ROOT / "backups"
    staging = base / f".staging-{ts}-{os.getpid()}"
    final = base / f"{ts}__pre-update"
    i = 1
    while final.exists():
        i += 1
        final = base / f"{ts}-{i}__pre-update"
    dest_db = staging / "records.db"
    try:
        staging.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(str(db))
        try:
            dst = sqlite3.connect(str(dest_db))
            try:
                with dst:
                    src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        chk = sqlite3.connect(str(dest_db))
        try:
            integrity = chk.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            chk.close()  # MUST close, or the staging dir can't be renamed on Windows
        if src_objs and _objects(dest_db) < src_objs:
            raise RuntimeError("snapshot incomplete (fewer objects than source)")
        ver = "?"
        try:
            ver = (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
        except OSError:
            pass
        (staging / "meta.json").write_text(json.dumps({
            "timestamp": ts, "reason": "pre-update", "version": ver,
            "db_bytes": dest_db.stat().st_size, "integrity": integrity,
            "objects": _objects(dest_db),
        }, indent=2), encoding="utf-8")
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    os.replace(staging, final)
    _prune_db_backups()
    return final.name


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

    # 0) Snapshot the database BEFORE touching anything, so the user always has a
    #    revert point. A genuine backup failure (disk full, permission) is a HARD
    #    STOP — we won't apply an update we couldn't take a safety copy for.
    if (ROOT / "data" / "records.db").exists():
        try:
            db_backup = backup_database()
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: could not safely back up your database ({e}).\n"
                  "       Your data was NOT changed and the update was NOT applied.\n"
                  "       Free up disk space or close the program, then try again.")
            shutil.rmtree(staging, ignore_errors=True)
            return 6
        if db_backup:
            print(f"Backed up your database to backups/{db_backup} (revert with restore_data.py).")

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

        # 2) Copy staged code over the live install. Skip preserved files AND
        #    every protected top-level name, so even a malformed zip that smuggled
        #    a data/ or backups/ dir can never overwrite the user's data.
        copied = 0
        for item in sorted(src.rglob("*")):
            rel = item.relative_to(src)
            if rel.parts and rel.parts[0] in SKIP_TOP:
                continue
            target = ROOT / rel
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
                copied += 1
    except Exception as e:  # noqa: BLE001
        print(f"ERROR while applying files: {e}\n       Rolling back to your "
              "previous version...")
        try:
            for item in backup.iterdir():
                target = ROOT / item.name
                if item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, target)
            print("       Your previous version has been restored. No data was changed.")
        except Exception as rb:  # noqa: BLE001
            print(f"       Automatic rollback also failed ({rb}). Your previous code "
                  f"is in .update_backup/{ts} — copy it back over this folder.")
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
    # Move the applied zip aside so neither the web nor the launcher path can
    # re-apply it (or pick a now-stale older zip) on the next run.
    try:
        applied = ROOT / ".applied_updates"
        applied.mkdir(parents=True, exist_ok=True)
        os.replace(zp, applied / zp.name)
    except OSError:
        pass
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
