"""Database behavior: migrations, upsert semantics, derived columns, search."""
from __future__ import annotations

import sqlite3

from records_tracker.database import Database


def test_backward_compat_migration_on_old_schema(tmp_path):
    """An old DB missing newer columns must migrate cleanly (the auto-update relies
    on this — users carry their data across versions)."""
    p = tmp_path / "old.db"
    con = sqlite3.connect(str(p))
    con.executescript(
        "CREATE TABLE requests (request_id TEXT PRIMARY KEY, rid INTEGER UNIQUE, "
        "status TEXT, final_state TEXT, first_seen_at TEXT NOT NULL, last_scraped_at TEXT NOT NULL);"
        "CREATE TABLE attachments (attachment_id INTEGER PRIMARY KEY, request_id TEXT, "
        "rid INTEGER, filename TEXT, download_status TEXT, first_seen_at TEXT NOT NULL);")
    con.execute("INSERT INTO requests VALUES ('P1', 1, 'open', 'open', '2025-01-01', '2025-01-01')")
    con.commit()
    con.close()

    db = Database(p)  # should add short_title, download_attempts, etc.
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(requests)")}
    acols = {r[1] for r in db._conn.execute("PRAGMA table_info(attachments)")}
    assert "short_title" in cols
    assert "download_attempts" in acols
    assert db._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.get_request("P1")["request_id"] == "P1"  # data preserved
    db.close()


def test_upsert_preserves_derived_columns(db):
    rid = db.get_all_requests()[0]["request_id"]
    before = db.get_request(rid)
    assert before["hours_to_first_reply"] is not None
    with db.transaction():
        db.upsert_request({"request_id": rid, "rid": before["rid"], "status": "CHANGED",
                           "final_state": before["final_state"], "department": "NEW",
                           "detail_url": before["detail_url"]})
    after = db.get_request(rid)
    assert after["status"] == "CHANGED" and after["department"] == "NEW"   # scraped cols updated
    assert after["hours_to_first_reply"] == before["hours_to_first_reply"]  # derived preserved
    assert after["first_seen_at"] == before["first_seen_at"]               # not clobbered


def test_recompute_first_reply_uses_substantive(db):
    rid = db.get_all_requests()[0]["request_id"]
    r = db.get_request(rid)
    # the seed's 3rd message is the first substantive support reply
    assert r["first_real_reply_time"] is not None
    assert r["first_auto_ack_time"] is not None
    assert r["hours_to_first_reply"] and r["hours_to_first_reply"] > 0


def test_full_text_search(db):
    assert db.search("rezoning")                       # matches a description
    assert db.search("body camera")                    # matches a description
    fee = db.search("312.50")
    assert fee and any(h["kind"] == "attachment" for r in fee for h in r["hits"])  # attachment text
    assert db.search("zzzznotpresent") == []
    assert db.search("") == []


def test_search_index_rebuilds_on_change(db):
    db.search("rezoning")  # build
    rid = db.get_all_requests()[1]["request_id"]
    db.set_short_title(rid, "uniquenicknametoken")
    hits = db.search("uniquenicknametoken")            # signature changed -> rebuilt
    assert hits and hits[0]["request_id"] == rid


def test_clear_open_ai_issues_keeps_user_issues(db):
    rid = db.get_all_requests()[0]["request_id"]
    db.add_compliance_issue({"request_id": rid, "issue_type": "manual", "description": "x",
                             "severity": "low", "identified_by": "user", "status": "open"})
    db.clear_open_ai_issues(rid)
    kinds = {i["identified_by"] for i in db.get_compliance_issues(request_id=rid)}
    assert "user" in kinds and "ai" not in kinds       # only AI-open issues cleared
