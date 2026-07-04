"""Shared pytest fixtures: an isolated, seeded database and a temp environment
so tests never touch the real data/ folder."""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta

import pytest

from records_tracker.database import Database


def seed(db: Database) -> list[str]:
    """Populate a small, realistic dataset. Returns the request ids."""
    ids = []
    base = datetime(2025, 1, 5)
    rows = [
        ("P120000-012025", 120000, "In Progress", "In Progress", "Police",
         "All emails about the 4th Street rezoning between the City Manager and developers."),
        ("P120137-022025", 120137, "Completed", "Completed", "City Clerk",
         "Body-worn camera footage and incident report for case 2025-0148291."),
        ("P120274-032025", 120274, "Denied", "Denied", "Legal",
         "Use-of-force reports filed by the Police Department in Q1 2025."),
    ]
    mid = 500000
    for i, (rid, rnum, status, final, dept, desc) in enumerate(rows):
        db.upsert_request({
            "request_id": rid, "rid": rnum, "status": status, "final_state": final,
            "request_type": "Public Records Request", "category": "Citizen",
            "department": dept, "records_type": "Electronic", "description": desc,
            "preferred_method": "email", "requester_email": "user@example.com",
            "detail_url": f"https://example.com/RequestEdit.aspx?rid={rnum}",
        })
        ids.append(rid)
        t = base + timedelta(days=i * 7)
        mid += 1
        db.upsert_message({"message_id": mid, "request_id": rid, "sent_at": t.isoformat(),
                           "sender": "Brad McCoy", "subject": "New request", "body": desc,
                           "sequence_num": 1, "is_auto_ack": 0})
        mid += 1
        db.upsert_message({"message_id": mid, "request_id": rid,
                           "sent_at": (t + timedelta(minutes=10)).isoformat(),
                           "sender": "STPETEFL Support", "subject": "Received",
                           "body": "Your request has been received.", "sequence_num": 2,
                           "is_auto_ack": 1})
        mid += 1
        db.upsert_message({"message_id": mid, "request_id": rid,
                           "sent_at": (t + timedelta(days=20)).isoformat(),
                           "sender": "STPETEFL Support", "subject": "Update",
                           "body": "We estimate a special service charge of $312.50.",
                           "sequence_num": 3, "is_auto_ack": 0})
        db.upsert_message_classification(mid, "substantive", 0.9, "first determination", "haiku")
        with db.transaction():
            db.recompute_first_reply(rid)
    # one attachment with extracted text on the first request
    db.upsert_attachment({"attachment_id": 900001, "request_id": ids[0], "rid": 120000,
                          "filename": "fee_estimate.pdf", "local_path": None,
                          "download_status": "pending", "file_size": None,
                          "downloaded_at": None, "error_message": None})
    db.mark_attachment_downloaded(900001, "/tmp/fee_estimate.pdf", 1234)
    db.upsert_attachment_text(900001, "This invoice quotes a fee of $312.50 for body camera footage.")
    db.add_compliance_issue({"request_id": ids[0], "statute_section": "119.07(4)(d)",
                             "issue_type": "excessive_fee", "severity": "high",
                             "description": "Fee appears to include review time.",
                             "ai_confidence": 0.8, "identified_by": "ai", "status": "open"})
    conv = db.create_conversation("request", f"Compliance audit — {ids[0]}", request_id=ids[0])
    db.add_conversation_message(conv, "user", "Audit please.")
    db.add_conversation_message(conv, "assistant", '{"issues": [], "overall_assessment": "ok"}')
    return ids


@pytest.fixture
def db(tmp_path):
    """A fresh, seeded Database at a temp path."""
    database = Database(tmp_path / "data" / "records.db")
    seed(database)
    yield database
    database.close()


@pytest.fixture
def temp_paths(tmp_path, monkeypatch):
    """Point every module's project_paths() at an isolated temp tree, and return
    the seeded paths dict + request ids."""
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    paths = {"root": tmp_path, "data": data, "downloads": data / "downloads",
             "database": data / "records.db", "excel": data / "records.xlsx",
             "logs": tmp_path / "logs"}
    sdb = Database(paths["database"])
    ids = seed(sdb)
    sdb.close()  # don't leave a connection open (restore tests need exclusive access)

    def fake_paths():
        return dict(paths)

    for name in ("records_tracker.config", "records_tracker.backup",
                 "records_tracker.updater", "server"):
        mod = importlib.import_module(name)
        if hasattr(mod, "project_paths"):
            monkeypatch.setattr(mod, "project_paths", fake_paths)
    paths["ids"] = ids
    return paths
