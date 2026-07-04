"""The updater applier: zip selection, layout detection, guards, data safety."""
from __future__ import annotations

import shutil
import sqlite3

import apply_update


def _release(d, version="2.0.0", server="NEW"):
    d.mkdir(parents=True, exist_ok=True)
    (d / "VERSION.txt").write_text(version)
    (d / "server.py").write_text(server)
    return d


def _zip(srcdir, dest):
    shutil.make_archive(str(dest.with_suffix("")), "zip", str(srcdir))


def test_find_zip_picks_highest_version(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_update, "ROOT", tmp_path)
    for n in ("update-1.2.0.zip", "update-1.10.0.zip", "update-1.9.0.zip"):
        (tmp_path / n).write_bytes(b"x")
    assert apply_update.find_zip().name == "update-1.10.0.zip"  # numeric, not lexicographic


def test_resolve_source_handles_flat_wrapped_and_bad(tmp_path):
    flat = _release(tmp_path / "flat")
    assert apply_update.resolve_source(flat) == flat
    wrapped = tmp_path / "wrapped"
    _release(wrapped / "RecordsTracker")
    assert apply_update.resolve_source(wrapped) == wrapped / "RecordsTracker"
    bad = tmp_path / "bad"
    (bad / "x").mkdir(parents=True)
    (bad / "x" / "readme.txt").write_text("hi")
    assert apply_update.resolve_source(bad) is None


def test_safe_removed_entry_guards():
    assert apply_update.safe_removed_entry("records_tracker/old.py")
    assert not apply_update.safe_removed_entry("../evil")
    assert not apply_update.safe_removed_entry("data/records.db")
    assert not apply_update.safe_removed_entry("/abs/path")
    assert not apply_update.safe_removed_entry("credentials.json")
    assert not apply_update.safe_removed_entry("backups/x")


def _install(tmp_path):
    (tmp_path / "VERSION.txt").write_text("1.0.0")
    (tmp_path / "server.py").write_text("OLD")
    (tmp_path / "credentials.json").write_text("SECRET")
    (tmp_path / "data").mkdir()
    con = sqlite3.connect(str(tmp_path / "data" / "records.db"))
    con.execute("CREATE TABLE t(x)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(20)])
    con.commit()
    con.close()


def test_full_apply_preserves_data_and_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_update, "ROOT", tmp_path)
    _install(tmp_path)
    _zip(_release(tmp_path / "rel", "2.0.0", "NEW"), tmp_path / "update-2.0.0.zip")

    assert apply_update.main() == 0
    assert (tmp_path / "VERSION.txt").read_text() == "2.0.0"
    assert (tmp_path / "server.py").read_text() == "NEW"
    assert (tmp_path / "credentials.json").read_text() == "SECRET"      # never touched
    con = sqlite3.connect(str(tmp_path / "data" / "records.db"))
    assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 20    # data preserved
    con.close()
    backs = list((tmp_path / "backups").iterdir())
    assert any("pre-update" in d.name for d in backs)                   # snapshot taken
    assert (tmp_path / ".applied_updates" / "update-2.0.0.zip").exists()  # zip archived


def test_apply_hard_stops_when_backup_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_update, "ROOT", tmp_path)
    _install(tmp_path)
    (tmp_path / "backups").write_text("blocking file, not a dir")       # snapshot can't be written
    _zip(_release(tmp_path / "rel", "2.0.0", "NEW"), tmp_path / "update-2.0.0.zip")

    assert apply_update.main() == 6                                     # refuses
    assert (tmp_path / "VERSION.txt").read_text() == "1.0.0"            # nothing changed
    assert (tmp_path / "server.py").read_text() == "OLD"
