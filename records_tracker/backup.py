"""Local backups of the user's data (the SQLite database).

The database is the irreplaceable state — requests, messages, AI classifications
and summaries, compliance issues, conversations, and overrides. Downloaded
attachment files are large and re-downloadable, and are never modified by an
update, so they are intentionally NOT copied here.

Safety properties (every one matters — a backup the user can't trust is worse
than none):
  * Snapshots are built in a TEMP dir and only renamed into place once the copy
    succeeded AND passed an integrity check AND actually contains data — so a
    half-written / zero-byte / corrupt snapshot is never offered for restore.
  * Pruning is integrity- and role-aware: it never deletes the most recent
    good backup, nor the most recent pre-restore safety copy.
  * Restore refuses to run while the program is still open (which would corrupt
    or silently truncate the live database), and swaps the file atomically.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import urllib.request
from datetime import datetime
from pathlib import Path

from .config import PROJECT_ROOT, project_paths

log = logging.getLogger(__name__)

KEEP_BACKUPS = 10


class BackupError(RuntimeError):
    """The backup engine failed (disk full, permission, etc.) — distinct from a
    successful snapshot of an already-corrupt database."""


def backups_root() -> Path:
    return PROJECT_ROOT / "backups"


def _local_version() -> str:
    try:
        return (PROJECT_ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return "?"


def _object_count(db: Path) -> int:
    con = sqlite3.connect(str(db))
    try:
        return con.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
    finally:
        con.close()


def _integrity(db: Path) -> str:
    con = sqlite3.connect(str(db))
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()


def make_db_backup(reason: str = "manual") -> dict | None:
    """Snapshot the database into backups/. Returns a meta dict, or None if there
    is no database yet. Raises BackupError if the snapshot engine fails (so the
    caller can report a real failure rather than a false success). A snapshot of
    an already-corrupt DB succeeds but is marked integrity != 'ok' in its meta."""
    paths = project_paths()
    db_path = paths["database"]
    if not db_path.exists():
        log.info("No database to back up yet.")
        return None

    src_objects = None
    try:
        src_objects = _object_count(db_path)
    except Exception:  # noqa: BLE001
        pass

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", reason).strip("-")[:40] or "backup"
    root = backups_root()
    # Build in a temp dir; only rename into the real name once it's verified.
    staging = root / f".staging-{ts}-{os.getpid()}"
    final = root / f"{ts}__{safe}"
    i = 1
    while final.exists():
        i += 1
        final = root / f"{ts}-{i}__{safe}"

    try:
        staging.mkdir(parents=True, exist_ok=True)
        dest_db = staging / "records.db"
        src = sqlite3.connect(str(db_path))
        try:
            dst = sqlite3.connect(str(dest_db))
            try:
                with dst:
                    src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        integrity = _integrity(dest_db)
        dst_objects = _object_count(dest_db)
        # Guard against a silently-empty snapshot of a non-empty DB (which would
        # pass integrity_check yet wipe everything if ever restored).
        if src_objects and dst_objects < src_objects:
            raise BackupError(
                f"snapshot looks incomplete ({dst_objects} of {src_objects} objects)")
        meta = {
            "timestamp": ts, "reason": reason, "version": _local_version(),
            "db_bytes": dest_db.stat().st_size, "integrity": integrity,
            "objects": dst_objects,
        }
        (staging / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(staging, ignore_errors=True)
        raise BackupError(str(e)) from e

    os.replace(staging, final)  # atomic commit of a verified backup
    if integrity != "ok":
        log.warning("Backup integrity check returned %r for %s", integrity, final.name)
    else:
        log.info("Backed up database -> %s (%d bytes)", final.name, meta["db_bytes"])
    prune_backups()
    meta["name"] = final.name
    return meta


def _read_meta(d: Path) -> dict | None:
    """Return a backup dir's meta, or None if the dir is incomplete (no
    records.db, zero-byte records.db, or no meta.json) — such dirs are never
    offered for restore and never counted toward pruning."""
    db = d / "records.db"
    mp = d / "meta.json"
    if not db.exists() or not mp.exists():
        return None
    try:
        if db.stat().st_size == 0:
            return None
    except OSError:
        return None
    meta = {"name": d.name, "reason": "?", "version": "?", "integrity": "?",
            "timestamp": d.name.split("__")[0]}
    try:
        meta.update(json.loads(mp.read_text(encoding="utf-8")))
    except Exception:
        pass
    meta["name"] = d.name
    if meta.get("db_bytes") is None:
        meta["db_bytes"] = db.stat().st_size
    return meta


def list_backups() -> list[dict]:
    root = backups_root()
    if not root.exists():
        return []
    out = []
    for d in root.iterdir():
        if not d.is_dir() or d.name.startswith(".staging-"):
            continue
        meta = _read_meta(d)
        if meta is not None:
            out.append(meta)
    out.sort(key=lambda m: m["name"], reverse=True)
    return out


def prune_backups(keep: int = KEEP_BACKUPS) -> int:
    """Keep the most recent `keep` backups, but ALWAYS also keep (a) the most
    recent integrity-ok backup and (b) the most recent pre-restore safety copy,
    even if they fall outside that window — so a restorable point and a restore-
    undo always survive. Incomplete dirs were already excluded by list_backups."""
    items = list_backups()  # newest first
    if not items:
        return 0
    keepset = {b["name"] for b in items[:keep]}
    newest_ok = next((b for b in items if b.get("integrity") == "ok"), None)
    if newest_ok:
        keepset.add(newest_ok["name"])
    newest_pre = next((b for b in items if b.get("reason") == "pre-restore"), None)
    if newest_pre:
        keepset.add(newest_pre["name"])
    removed = 0
    for b in items:
        if b["name"] not in keepset:
            shutil.rmtree(backups_root() / b["name"], ignore_errors=True)
            removed += 1
    return removed


def _server_port() -> int:
    try:
        lock = json.loads((project_paths()["data"] / ".server.lock").read_text(encoding="utf-8"))
        return int(lock.get("port") or 5000)
    except Exception:
        return 5000


def app_appears_running() -> bool:
    """True if the web UI seems to be running (so a restore must NOT proceed).
    Probes /healthz on the recorded/default port — authoritative when reachable."""
    for port in {_server_port(), 5000}:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            continue
    return False


def restore_db_backup(name: str) -> dict:
    """Restore records.db from a named backup. SAFE:
      * refuses if the program appears to still be running,
      * verifies the backup's integrity before touching anything,
      * snapshots the CURRENT database first (reason='pre-restore') so the
        restore itself is reversible,
      * folds/clears the live WAL, then swaps the file ATOMICALLY (os.replace).
    """
    paths = project_paths()
    src_dir = backups_root() / name
    src_db = src_dir / "records.db"
    if not src_db.exists():
        raise FileNotFoundError(f"No backup named {name!r} (expected {src_db}).")
    if app_appears_running():
        raise RuntimeError(
            "The Records Tracker window still appears to be open. Close it "
            "completely (the black Start window), then run Restore again. "
            "Nothing was changed.")
    if _integrity(src_db) != "ok":
        raise RuntimeError(f"Backup {name!r} fails its integrity check; not restoring.")

    db_path = paths["database"]
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Capture the source backup into a temp file FIRST and verify it — before
    # anything that could prune it (the pre-restore snapshot below runs prune,
    # which could otherwise evict the very backup we're restoring).
    tmp = db_path.with_name(db_path.name + ".restore-tmp")
    shutil.copy2(src_db, tmp)
    if _integrity(tmp) != "ok":  # paranoia: verify the copy landed intact
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Restored copy failed verification; live data left unchanged.")

    safety = None
    if db_path.exists():
        safety = make_db_backup(reason="pre-restore")  # so a restore can be undone
        # Fold any committed WAL into the main file and clear it. If something
        # still holds a write lock this raises -> we abort before changing data.
        con = sqlite3.connect(str(db_path), timeout=5)
        try:
            con.execute("PRAGMA busy_timeout = 5000")
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            con.close()
        for sidecar in (db_path.with_name(db_path.name + "-wal"),
                        db_path.with_name(db_path.name + "-shm")):
            try:
                sidecar.unlink()
            except OSError:
                pass

    os.replace(tmp, db_path)  # atomic: records.db is never left partial
    log.info("Restored database from backup %s", name)
    return {"restored": name, "safety_backup": (safety or {}).get("name")}
