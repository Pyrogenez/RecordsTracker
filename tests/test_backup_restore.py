"""Data-safety: backup integrity, crash-safety, prune protection, restore."""
from __future__ import annotations

import json
import shutil

import pytest

from records_tracker import backup
from records_tracker.database import Database


def test_backup_is_verified(temp_paths):
    m = backup.make_db_backup("test")
    assert m and m["integrity"] == "ok" and m["objects"] > 0
    assert (backup.backups_root() / m["name"] / "records.db").exists()
    assert (backup.backups_root() / m["name"] / "meta.json").exists()


def test_backup_crash_safe_leaves_nothing(temp_paths):
    # Corrupt the source so the snapshot engine fails partway.
    temp_paths["database"].write_bytes(b"this is not a sqlite database")
    with pytest.raises(backup.BackupError):
        backup.make_db_backup("willfail")
    root = backup.backups_root()
    leftover = [d.name for d in root.iterdir()] if root.exists() else []
    assert not any("willfail" in n for n in leftover)      # no committed bad backup
    assert not any(n.startswith(".staging-") for n in leftover)  # no orphan staging dir


def test_list_ignores_incomplete_dirs(temp_paths):
    backup.make_db_backup("good")
    bad = backup.backups_root() / "20200101-000000__bad"
    bad.mkdir(parents=True)
    (bad / "records.db").write_bytes(b"")                 # zero-byte, no meta.json
    assert "20200101-000000__bad" not in {b["name"] for b in backup.list_backups()}


def test_prune_protects_newest_good_backup(temp_paths):
    good = backup.make_db_backup("good")                  # the only integrity-ok one
    root = backup.backups_root()
    src = root / good["name"] / "records.db"
    for i in range(13):                                   # 13 NEWER corrupt backups
        d = root / f"20990101-0000{i:02d}__bad"
        d.mkdir(parents=True)
        shutil.copy2(src, d / "records.db")
        (d / "meta.json").write_text(json.dumps(
            {"timestamp": f"20990101-0000{i:02d}", "reason": "manual", "integrity": "corrupt"}))
    backup.prune_backups(keep=10)
    remaining = backup.list_backups()
    assert any(b.get("integrity") == "ok" for b in remaining)  # good one never evicted


def test_restore_round_trip(temp_paths, monkeypatch):
    monkeypatch.setattr(backup, "app_appears_running", lambda: False)
    snap = backup.make_db_backup("snap")
    db = Database(temp_paths["database"])
    rid = db.get_all_requests()[0]["request_id"]
    before = db.counts()["compliance_issues_total"]
    db.add_compliance_issue({"request_id": rid, "issue_type": "TEMP", "description": "x",
                             "severity": "low", "identified_by": "user", "status": "open"})
    assert db.counts()["compliance_issues_total"] == before + 1
    db.close()

    res = backup.restore_db_backup(snap["name"])
    db = Database(temp_paths["database"])
    assert db.counts()["compliance_issues_total"] == before      # mutation reverted
    db.close()
    assert res["safety_backup"]                                  # current data saved first


def test_restore_refused_while_app_running(temp_paths, monkeypatch):
    snap = backup.make_db_backup("snap")
    monkeypatch.setattr(backup, "app_appears_running", lambda: True)
    with pytest.raises(RuntimeError, match="still appears"):
        backup.restore_db_backup(snap["name"])
