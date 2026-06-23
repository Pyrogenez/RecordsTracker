"""SQLite storage layer for scraped request data."""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

log = logging.getLogger(__name__)

# A 'failed' attachment is retried on subsequent runs until it has been
# attempted this many times, after which it is considered permanently failed
# and stops generating retry noise every run.
DEFAULT_MAX_DOWNLOAD_ATTEMPTS = 5


def is_support_sender(sender: str | None) -> bool:
    """True if a message sender is the city's records-support account.

    Centralizes a check that used to be duplicated (and inconsistent) across
    database.py, analyze.py and a template. Conservative on purpose — exact
    match or an 'stpetefl support' prefix — so a requester whose own display
    name merely contains the word "support" is never misbucketed as the agency.
    """
    s = (sender or "").strip().lower()
    return s == "stpetefl support" or s.startswith("stpetefl support")

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id          TEXT PRIMARY KEY,
    rid                 INTEGER NOT NULL UNIQUE,
    status              TEXT,
    final_state         TEXT,
    request_type        TEXT,
    category            TEXT,
    department          TEXT,
    records_type        TEXT,
    description         TEXT,
    preferred_method    TEXT,
    requester_email     TEXT,
    first_seen_at       TEXT NOT NULL,
    last_scraped_at     TEXT NOT NULL,
    last_modified_at    TEXT,
    submission_time     TEXT,
    first_auto_ack_time TEXT,
    first_real_reply_time TEXT,
    hours_to_first_reply REAL,
    detail_url          TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    message_id   INTEGER PRIMARY KEY,
    request_id   TEXT NOT NULL REFERENCES requests(request_id),
    sent_at      TEXT NOT NULL,
    sender       TEXT NOT NULL,
    subject      TEXT,
    body         TEXT,
    sequence_num INTEGER,
    is_auto_ack  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_request ON messages(request_id, sequence_num);

CREATE TABLE IF NOT EXISTS attachments (
    attachment_id     INTEGER PRIMARY KEY,
    request_id        TEXT NOT NULL REFERENCES requests(request_id),
    rid               INTEGER NOT NULL,
    filename          TEXT NOT NULL,
    local_path        TEXT,
    download_status   TEXT NOT NULL DEFAULT 'pending',
    file_size         INTEGER,
    downloaded_at     TEXT,
    error_message     TEXT,
    download_attempts INTEGER NOT NULL DEFAULT 0,
    first_seen_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_request ON attachments(request_id);

CREATE TABLE IF NOT EXISTS overrides (
    request_id                 TEXT PRIMARY KEY REFERENCES requests(request_id),
    first_real_reply_message_id INTEGER,
    is_closed                  INTEGER NOT NULL DEFAULT 0,
    notes                      TEXT,
    updated_at                 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    mode               TEXT,
    requests_scraped   INTEGER DEFAULT 0,
    requests_skipped   INTEGER DEFAULT 0,
    new_requests       INTEGER DEFAULT 0,
    new_messages       INTEGER DEFAULT 0,
    new_attachments    INTEGER DEFAULT 0,
    error              TEXT
);

-- Text extracted from downloaded attachment files (populated by analyze.py extract).
CREATE TABLE IF NOT EXISTS attachment_text (
    attachment_id  INTEGER PRIMARY KEY REFERENCES attachments(attachment_id),
    extracted_text TEXT,
    word_count     INTEGER,
    extracted_at   TEXT NOT NULL,
    error          TEXT
);

-- AI-assigned classification of messages (populated by analyze.py classify).
CREATE TABLE IF NOT EXISTS message_classifications (
    message_id    INTEGER PRIMARY KEY REFERENCES messages(message_id),
    classification TEXT NOT NULL,   -- 'auto_ack' | 'status_update' | 'substantive' | 'other'
    confidence    REAL,
    reasoning     TEXT,
    model         TEXT,
    classified_at TEXT NOT NULL
);

-- AI-generated per-request summaries (populated by analyze.py summarize).
CREATE TABLE IF NOT EXISTS request_summaries (
    request_id  TEXT PRIMARY KEY REFERENCES requests(request_id),
    summary     TEXT NOT NULL,
    model       TEXT,
    updated_at  TEXT NOT NULL
);

-- Saved AI conversation threads. scope='request' threads belong to one
-- request; scope='global' threads are cross-request analyses.
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope           TEXT NOT NULL CHECK (scope IN ('request','global')),
    request_id      TEXT REFERENCES requests(request_id),
    title           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_request ON conversations(request_id);

CREATE TABLE IF NOT EXISTS conversation_messages (
    message_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    content         TEXT NOT NULL,
    model           TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_convmsg_conv ON conversation_messages(conversation_id, message_id);

-- Compliance issues: specific places where the city may have failed to
-- follow FL Public Records Law (Chapter 119) or related statutes.
CREATE TABLE IF NOT EXISTS compliance_issues (
    issue_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id      TEXT NOT NULL REFERENCES requests(request_id),
    statute_section TEXT,             -- e.g. "119.07(1)(c)"
    issue_type      TEXT NOT NULL,    -- e.g. "unreasonable_delay", "unlawful_denial", "excessive_fee"
    severity        TEXT,             -- 'low' | 'medium' | 'high'
    description     TEXT NOT NULL,
    evidence        TEXT,             -- quote / message_id / attachment ref
    ai_confidence   REAL,
    identified_by   TEXT NOT NULL,    -- 'ai' | 'user'
    model           TEXT,
    status          TEXT DEFAULT 'open',  -- 'open' | 'resolved' | 'dismissed'
    user_notes      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_compliance_request ON compliance_issues(request_id);
CREATE INDEX IF NOT EXISTS idx_compliance_status ON compliance_issues(status);
"""

# Applied to already-existing DBs at open time. Each is an idempotent additive
# ALTER; "duplicate column name" on re-run is expected and ignored, anything
# else is logged. Never destructive — existing populated DBs must keep working.
_MIGRATIONS: list[str] = [
    "ALTER TABLE overrides ADD COLUMN is_closed INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE runs ADD COLUMN mode TEXT",
    "ALTER TABLE runs ADD COLUMN requests_skipped INTEGER DEFAULT 0",
    "ALTER TABLE attachments ADD COLUMN download_attempts INTEGER NOT NULL DEFAULT 0",
]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Thin wrapper around sqlite3 with the operations we actually use."""

    def __init__(self, db_path: Path, *, ensure_schema: bool = True):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        # WAL lets the web UI keep reading while a scrape writes (and vice
        # versa) instead of raising "database is locked"; busy_timeout makes any
        # unavoidable wait-on-lock explicit rather than failing instantly. Both
        # are persisted/idempotent and safe on an existing populated DB. (May
        # be unavailable on some network filesystems — degrade gracefully.)
        try:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA busy_timeout = 5000")
        except sqlite3.OperationalError as exc:
            log.warning("Could not enable WAL/busy_timeout: %s", exc)
        # ensure_schema=False lets the web UI open a per-request connection
        # without re-running the full schema script + migrations every page load
        # (create_app runs it once at startup instead).
        if ensure_schema:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create tables and apply idempotent migrations. Cheap to re-run."""
        self._conn.executescript(SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                # "duplicate column name" is the expected idempotent re-run.
                # Anything else (typo, missing table, disk error) is a real
                # problem and should not be swallowed silently.
                if "duplicate column name" not in str(exc).lower():
                    log.warning("Migration step failed (%s): %s", stmt, exc)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ------- requests -------
    # Columns supplied by the scraper. The derived analytics columns
    # (submission_time, first_*_time, hours_to_first_reply, last_modified_at)
    # are deliberately NOT here — they are owned by recompute_first_reply().
    SCRAPED_COLS = [
        "rid", "status", "final_state", "request_type", "category",
        "department", "records_type", "description", "preferred_method",
        "requester_email", "detail_url",
    ]

    def upsert_request(self, data: dict) -> bool:
        """Insert or update a request. Returns True if this is a new request.

        On update we touch only scraped columns + last_scraped_at, preserving
        first_seen_at and the derived analytics columns. The previous
        INSERT OR REPLACE nulled every derived column on every scrape and relied
        on the immediately-following recompute_first_reply to repair them — a
        brittle implicit contract this avoids.
        """
        request_id = data["request_id"]
        now = now_utc()
        existing = self._conn.execute(
            "SELECT request_id FROM requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if existing is not None:
            sets = ", ".join(f"{c} = :{c}" for c in self.SCRAPED_COLS)
            params = {c: data.get(c) for c in self.SCRAPED_COLS}
            params["request_id"] = request_id
            params["last_scraped_at"] = now
            self._conn.execute(
                f"UPDATE requests SET {sets}, last_scraped_at = :last_scraped_at "
                "WHERE request_id = :request_id",
                params,
            )
            return False

        # New request_id. Guard against a `rid` UNIQUE collision caused by a
        # prior rid-only --ids-file placeholder ("P<rid>") being upgraded to its
        # canonical key ("P<rid>-MMDDYY"). Self-heal a childless placeholder;
        # otherwise keep the existing row and skip rather than aborting the run.
        rid = data.get("rid")
        clash = self._conn.execute(
            "SELECT request_id FROM requests WHERE rid = ? AND request_id != ?",
            (rid, request_id),
        ).fetchone()
        if clash is not None:
            old_id = clash["request_id"]
            if self._request_has_children(old_id):
                log.warning(
                    "Request %s shares rid=%s with existing %s which already has "
                    "messages/attachments; keeping the existing row and skipping "
                    "the new key.", request_id, rid, old_id,
                )
                return False
            self._conn.execute("DELETE FROM overrides WHERE request_id = ?", (old_id,))
            self._conn.execute("DELETE FROM requests WHERE request_id = ?", (old_id,))
            log.info("Reconciled placeholder %s -> %s (rid=%s)", old_id, request_id, rid)

        insert_cols = ["request_id", "first_seen_at", "last_scraped_at"] + self.SCRAPED_COLS
        params = {c: data.get(c) for c in self.SCRAPED_COLS}
        params["request_id"] = request_id
        params["first_seen_at"] = data.get("first_seen_at") or now
        params["last_scraped_at"] = now
        placeholders = ",".join(f":{c}" for c in insert_cols)
        self._conn.execute(
            f"INSERT INTO requests ({','.join(insert_cols)}) VALUES ({placeholders})",
            params,
        )
        return True

    def _request_has_children(self, request_id: str) -> bool:
        for tbl in ("messages", "attachments"):
            if self._conn.execute(
                f"SELECT 1 FROM {tbl} WHERE request_id = ? LIMIT 1", (request_id,)
            ).fetchone():
                return True
        return False

    def upsert_message(self, data: dict) -> bool:
        """Insert a message; skip if already present. Returns True if new."""
        existing = self._conn.execute(
            "SELECT message_id FROM messages WHERE message_id = ?",
            (data["message_id"],),
        ).fetchone()
        if existing is not None:
            # Update body/subject in case we had a truncated version before
            self._conn.execute(
                "UPDATE messages SET subject=:subject, body=:body, "
                "is_auto_ack=:is_auto_ack, sequence_num=:sequence_num "
                "WHERE message_id=:message_id",
                data,
            )
            return False
        self._conn.execute(
            "INSERT INTO messages (message_id, request_id, sent_at, sender, "
            "subject, body, sequence_num, is_auto_ack) VALUES "
            "(:message_id, :request_id, :sent_at, :sender, :subject, :body, "
            ":sequence_num, :is_auto_ack)",
            data,
        )
        return True

    def upsert_attachment(self, data: dict) -> bool:
        """Insert attachment record (metadata only). Returns True if new."""
        existing = self._conn.execute(
            "SELECT attachment_id FROM attachments WHERE attachment_id = ?",
            (data["attachment_id"],),
        ).fetchone()
        if existing is not None:
            return False
        data.setdefault("first_seen_at", now_utc())
        data.setdefault("download_status", "pending")
        self._conn.execute(
            "INSERT INTO attachments (attachment_id, request_id, rid, filename, "
            "local_path, download_status, file_size, downloaded_at, error_message, "
            "first_seen_at) VALUES (:attachment_id, :request_id, :rid, :filename, "
            ":local_path, :download_status, :file_size, :downloaded_at, :error_message, "
            ":first_seen_at)",
            data,
        )
        return True

    def mark_attachment_downloaded(self, attachment_id: int, local_path: str,
                                   file_size: int) -> None:
        self._conn.execute(
            "UPDATE attachments SET download_status='downloaded', local_path=?, "
            "file_size=?, downloaded_at=?, error_message=NULL WHERE attachment_id=?",
            (local_path, file_size, now_utc(), attachment_id),
        )
        self._conn.commit()

    def mark_attachment_failed(self, attachment_id: int, error_message: str) -> None:
        self._conn.execute(
            "UPDATE attachments SET download_status='failed', error_message=?, "
            "downloaded_at=?, download_attempts = download_attempts + 1 "
            "WHERE attachment_id=?",
            (error_message, now_utc(), attachment_id),
        )
        self._conn.commit()

    def get_pending_attachments(
        self, request_id: str | None = None,
        max_attempts: int = DEFAULT_MAX_DOWNLOAD_ATTEMPTS,
    ) -> list[dict]:
        """Attachments still worth attempting: all 'pending', plus 'failed' ones
        that haven't yet exhausted max_attempts. A permanently-failed attachment
        stops being retried every run (and is surfaced in counts())."""
        q = (
            "SELECT * FROM attachments WHERE download_status='pending' "
            "OR (download_status='failed' AND download_attempts < ?)"
        )
        params: list = [max_attempts]
        if request_id:
            q += " AND request_id = ?"
            params.append(request_id)
        rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def reset_failed_downloads(self, request_id: str | None = None) -> int:
        """Escape hatch: clear the attempt counter on failed attachments so the
        next run retries them. Returns how many were reset."""
        q = "UPDATE attachments SET download_attempts = 0 WHERE download_status='failed'"
        params: tuple = ()
        if request_id:
            q += " AND request_id = ?"
            params = (request_id,)
        cur = self._conn.execute(q, params)
        self._conn.commit()
        return cur.rowcount

    def get_all_requests(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM requests ORDER BY rid DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_request(self, request_id: str) -> dict | None:
        """Fetch a single request row (avoids scanning all requests to find one)."""
        row = self._conn.execute(
            "SELECT * FROM requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_attachments_for_request(self, request_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM attachments WHERE request_id = ? ORDER BY attachment_id",
            (request_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_attachment(self, attachment_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM attachments WHERE attachment_id = ?", (attachment_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_messages_for_request(self, request_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE request_id = ? ORDER BY sent_at ASC",
            (request_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_messages(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM messages ORDER BY request_id, sent_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_attachments(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM attachments ORDER BY request_id, attachment_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_override(self, request_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM overrides WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        return dict(row) if row else None

    def upsert_override(self, request_id: str, first_real_reply_message_id: int | None,
                        notes: str | None, is_closed: bool = False) -> None:
        self._conn.execute(
            "INSERT INTO overrides (request_id, first_real_reply_message_id, "
            "is_closed, notes, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(request_id) DO UPDATE SET "
            "first_real_reply_message_id=excluded.first_real_reply_message_id, "
            "is_closed=excluded.is_closed, "
            "notes=excluded.notes, updated_at=excluded.updated_at",
            (request_id, first_real_reply_message_id, 1 if is_closed else 0,
             notes, now_utc()),
        )
        self._conn.commit()

    @staticmethod
    def _final_state_is_closed(portal_final_state: str | None) -> bool:
        if portal_final_state:
            return portal_final_state.strip().lower() in {
                "completed", "closed", "fulfilled", "denied",
            }
        return False

    def is_request_closed(self, request_id: str, portal_final_state: str | None,
                          *, override_closed: bool | None = None) -> bool:
        """A request is considered closed if either (a) the portal reports
        'Completed' / 'Closed' / 'Fulfilled' / 'Denied' as its final_state, OR
        (b) the user has marked it closed in the overrides table.

        Pass override_closed when the caller already has the override flag (e.g.
        from a JOIN) to avoid an extra per-row query."""
        if override_closed is None:
            ov = self.get_override(request_id)
            override_closed = bool(ov and ov.get("is_closed"))
        if override_closed:
            return True
        return self._final_state_is_closed(portal_final_state)

    def request_exists(self, request_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        return row is not None

    def get_open_request_ids(self) -> set[str]:
        """Set of request IDs that are NOT closed (portal or override)."""
        rows = self._conn.execute(
            "SELECT r.request_id, r.final_state, o.is_closed AS override_closed "
            "FROM requests r LEFT JOIN overrides o USING (request_id)"
        ).fetchall()
        out: set[str] = set()
        for r in rows:
            if self.is_request_closed(r["request_id"], r["final_state"],
                                      override_closed=bool(r["override_closed"])):
                continue
            out.add(r["request_id"])
        return out

    def get_open_requests(self) -> list[dict]:
        """All open requests with full row data (request_id, rid, status,
        final_state, detail_url, etc.). Used by incremental scraping to
        refresh each open record via its detail page."""
        rows = self._conn.execute(
            "SELECT r.*, o.is_closed AS override_closed "
            "FROM requests r LEFT JOIN overrides o USING (request_id) "
            "ORDER BY r.rid DESC"
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            if self.is_request_closed(d["request_id"], d.get("final_state"),
                                      override_closed=bool(d.get("override_closed"))):
                continue
            out.append(d)
        return out

    def get_all_overrides(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM overrides").fetchall()
        return [dict(r) for r in rows]

    def start_run(self, mode: str = "incremental") -> int:
        cur = self._conn.execute(
            "INSERT INTO runs (started_at, mode) VALUES (?, ?)",
            (now_utc(), mode),
        )
        self._conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, *, requests_scraped: int,
                   requests_skipped: int, new_requests: int,
                   new_messages: int, new_attachments: int,
                   error: str | None = None) -> None:
        self._conn.execute(
            "UPDATE runs SET finished_at=?, requests_scraped=?, requests_skipped=?, "
            "new_requests=?, new_messages=?, new_attachments=?, error=? WHERE run_id=?",
            (now_utc(), requests_scraped, requests_skipped, new_requests,
             new_messages, new_attachments, error, run_id),
        )
        self._conn.commit()

    def get_last_run(self) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def get_recent_runs(self, limit: int = 25) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def has_baseline_run(self) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM runs WHERE mode = 'full' AND error IS NULL LIMIT 1"
        ).fetchone()
        return row is not None

    # ---- attachment text ----
    def upsert_attachment_text(self, attachment_id: int, text: str | None,
                               error: str | None = None) -> None:
        word_count = len(text.split()) if text else 0
        self._conn.execute(
            "INSERT INTO attachment_text (attachment_id, extracted_text, word_count, "
            "extracted_at, error) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(attachment_id) DO UPDATE SET "
            "extracted_text=excluded.extracted_text, "
            "word_count=excluded.word_count, "
            "extracted_at=excluded.extracted_at, "
            "error=excluded.error",
            (attachment_id, text, word_count, now_utc(), error),
        )
        self._conn.commit()

    def get_attachment_text(self, attachment_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM attachment_text WHERE attachment_id = ?",
            (attachment_id,),
        ).fetchone()
        return dict(row) if row else None

    def attachments_needing_text(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT a.* FROM attachments a "
            "LEFT JOIN attachment_text t USING (attachment_id) "
            "WHERE a.download_status = 'downloaded' AND "
            "      (t.attachment_id IS NULL OR t.error IS NOT NULL)"
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- message classifications ----
    def upsert_message_classification(self, message_id: int, classification: str,
                                      confidence: float | None, reasoning: str | None,
                                      model: str | None) -> None:
        self._conn.execute(
            "INSERT INTO message_classifications (message_id, classification, "
            "confidence, reasoning, model, classified_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(message_id) DO UPDATE SET "
            "classification=excluded.classification, "
            "confidence=excluded.confidence, "
            "reasoning=excluded.reasoning, model=excluded.model, "
            "classified_at=excluded.classified_at",
            (message_id, classification, confidence, reasoning, model, now_utc()),
        )
        self._conn.commit()

    def messages_needing_classification(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT m.* FROM messages m "
            "LEFT JOIN message_classifications c USING (message_id) "
            "WHERE c.message_id IS NULL ORDER BY m.sent_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_message_classification(self, message_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM message_classifications WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        return dict(row) if row else None

    # ---- request summaries ----
    def upsert_request_summary(self, request_id: str, summary: str,
                               model: str | None) -> None:
        self._conn.execute(
            "INSERT INTO request_summaries (request_id, summary, model, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(request_id) DO UPDATE SET "
            "summary=excluded.summary, model=excluded.model, "
            "updated_at=excluded.updated_at",
            (request_id, summary, model, now_utc()),
        )
        self._conn.commit()

    def get_request_summary(self, request_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM request_summaries WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_all_request_summaries(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM request_summaries"
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- conversations ----
    def create_conversation(self, scope: str, title: str,
                            request_id: str | None = None,
                            *, commit: bool = True) -> int:
        if scope not in ("request", "global"):
            raise ValueError(f"invalid scope: {scope}")
        if scope == "request" and not request_id:
            raise ValueError("request_id required for scope='request'")
        now = now_utc()
        cur = self._conn.execute(
            "INSERT INTO conversations (scope, request_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (scope, request_id, title, now, now),
        )
        if commit:
            self._conn.commit()
        return cur.lastrowid

    def rename_conversation(self, conversation_id: int, title: str) -> None:
        self._conn.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE conversation_id = ?",
            (title, now_utc(), conversation_id),
        )
        self._conn.commit()

    def delete_conversation(self, conversation_id: int) -> None:
        # ON DELETE CASCADE handles messages
        self._conn.execute(
            "DELETE FROM conversation_messages WHERE conversation_id = ?",
            (conversation_id,),
        )
        self._conn.execute(
            "DELETE FROM conversations WHERE conversation_id = ?", (conversation_id,),
        )
        self._conn.commit()

    def get_conversation(self, conversation_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_conversations(self, scope: str | None = None,
                           request_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM conversations WHERE 1=1"
        params: list = []
        if scope:
            q += " AND scope = ?"
            params.append(scope)
        if request_id:
            q += " AND request_id = ?"
            params.append(request_id)
        q += " ORDER BY updated_at DESC"
        rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def add_conversation_message(self, conversation_id: int, role: str,
                                 content: str, model: str | None = None,
                                 *, commit: bool = True) -> int:
        if role not in ("user", "assistant", "system"):
            raise ValueError(f"invalid role: {role}")
        now = now_utc()
        cur = self._conn.execute(
            "INSERT INTO conversation_messages (conversation_id, role, content, "
            "model, created_at) VALUES (?, ?, ?, ?, ?)",
            (conversation_id, role, content, model, now),
        )
        self._conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
            (now, conversation_id),
        )
        if commit:
            self._conn.commit()
        return cur.lastrowid

    def get_conversation_messages(self, conversation_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM conversation_messages WHERE conversation_id = ? "
            "ORDER BY message_id ASC",
            (conversation_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- compliance issues ----
    def add_compliance_issue(self, data: dict, *, commit: bool = True) -> int:
        now = now_utc()
        data.setdefault("created_at", now)
        data.setdefault("updated_at", now)
        data.setdefault("status", "open")
        data.setdefault("identified_by", "ai")
        cols = [
            "request_id", "statute_section", "issue_type", "severity",
            "description", "evidence", "ai_confidence", "identified_by",
            "model", "status", "user_notes", "created_at", "updated_at",
        ]
        merged = {c: data.get(c) for c in cols}
        placeholders = ",".join(f":{c}" for c in cols)
        cur = self._conn.execute(
            f"INSERT INTO compliance_issues ({','.join(cols)}) VALUES ({placeholders})",
            merged,
        )
        if commit:
            self._conn.commit()
        return cur.lastrowid

    def clear_open_ai_issues(self, request_id: str, *, commit: bool = True) -> int:
        """Delete still-open, AI-identified issues for a request so a fresh audit
        replaces them instead of duplicating. User-logged issues and any the user
        has already resolved/dismissed (or edited) are left untouched."""
        cur = self._conn.execute(
            "DELETE FROM compliance_issues WHERE request_id = ? "
            "AND identified_by = 'ai' AND status = 'open'",
            (request_id,),
        )
        if commit:
            self._conn.commit()
        return cur.rowcount

    def update_compliance_issue(self, issue_id: int, **fields) -> None:
        if not fields:
            return
        allowed = {
            "statute_section", "issue_type", "severity", "description",
            "evidence", "ai_confidence", "status", "user_notes",
        }
        sets = ", ".join(f"{k} = ?" for k in fields if k in allowed)
        if not sets:
            return
        values = [fields[k] for k in fields if k in allowed]
        values.append(now_utc())
        values.append(issue_id)
        self._conn.execute(
            f"UPDATE compliance_issues SET {sets}, updated_at = ? WHERE issue_id = ?",
            values,
        )
        self._conn.commit()

    def delete_compliance_issue(self, issue_id: int) -> None:
        self._conn.execute(
            "DELETE FROM compliance_issues WHERE issue_id = ?", (issue_id,),
        )
        self._conn.commit()

    def get_compliance_issues(self, request_id: str | None = None,
                              status: str | None = None) -> list[dict]:
        q = "SELECT * FROM compliance_issues WHERE 1=1"
        params: list = []
        if request_id:
            q += " AND request_id = ?"
            params.append(request_id)
        if status:
            q += " AND status = ?"
            params.append(status)
        q += " ORDER BY created_at DESC"
        rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    # ---- stats ----
    def counts(self) -> dict:
        def one(q: str) -> int:
            return self._conn.execute(q).fetchone()[0]
        return {
            "total_requests": one("SELECT COUNT(*) FROM requests"),
            "total_messages": one("SELECT COUNT(*) FROM messages"),
            "total_attachments": one("SELECT COUNT(*) FROM attachments"),
            "downloaded_attachments": one(
                "SELECT COUNT(*) FROM attachments WHERE download_status='downloaded'"
            ),
            "pending_attachments": one(
                "SELECT COUNT(*) FROM attachments WHERE download_status IN "
                "('pending','failed')"
            ),
            "failed_attachments": one(
                "SELECT COUNT(*) FROM attachments WHERE download_status='failed' "
                f"AND download_attempts >= {DEFAULT_MAX_DOWNLOAD_ATTEMPTS}"
            ),
            "open_requests": len(self.get_open_request_ids()),
            "user_closed_requests": one(
                "SELECT COUNT(*) FROM overrides WHERE is_closed = 1"
            ),
            "attachments_with_text": one(
                "SELECT COUNT(*) FROM attachment_text WHERE error IS NULL"
            ),
            "classified_messages": one(
                "SELECT COUNT(*) FROM message_classifications"
            ),
            "summarized_requests": one(
                "SELECT COUNT(*) FROM request_summaries"
            ),
            "conversations": one("SELECT COUNT(*) FROM conversations"),
            "compliance_issues_open": one(
                "SELECT COUNT(*) FROM compliance_issues WHERE status = 'open'"
            ),
            "compliance_issues_total": one(
                "SELECT COUNT(*) FROM compliance_issues"
            ),
        }

    def recompute_first_reply(self, request_id: str) -> None:
        """Derive submission_time, first_auto_ack_time, first_real_reply_time
        and hours_to_first_reply for a request based on stored messages and any
        user override. Idempotent — safe to call every scrape."""
        messages = self.get_messages_for_request(request_id)
        if not messages:
            return
        override = self.get_override(request_id)

        # Heuristic: first message = submission; first STPETEFL message = auto-ack;
        # next STPETEFL message = first real reply (unless overridden).
        first_msg = messages[0]
        submission_time = None if is_support_sender(first_msg["sender"]) else first_msg["sent_at"]
        if submission_time is None:
            # Requester might not appear; fall back to earliest message timestamp
            submission_time = first_msg["sent_at"]

        support_msgs = [m for m in messages if is_support_sender(m["sender"])]
        first_auto_ack = support_msgs[0]["sent_at"] if support_msgs else None

        first_real_reply = None
        if override and override.get("first_real_reply_message_id"):
            mid = override["first_real_reply_message_id"]
            match = next((m for m in messages if m["message_id"] == mid), None)
            if match:
                first_real_reply = match["sent_at"]
        if first_real_reply is None:
            # Prefer AI classifications: find first 'substantive' support message.
            for m in support_msgs:
                cls = self.get_message_classification(m["message_id"])
                if cls and cls["classification"] == "substantive":
                    first_real_reply = m["sent_at"]
                    break
        if first_real_reply is None and len(support_msgs) >= 2:
            # Fallback heuristic: second support message is the first real reply
            first_real_reply = support_msgs[1]["sent_at"]

        hours = None
        if submission_time and first_real_reply:
            try:
                from dateutil import parser as _p
                start = _p.parse(submission_time)
                end = _p.parse(first_real_reply)
                hours = round((end - start).total_seconds() / 3600.0, 2)
            except Exception:
                hours = None

        last_modified = messages[-1]["sent_at"] if messages else None

        self._conn.execute(
            "UPDATE requests SET submission_time=?, first_auto_ack_time=?, "
            "first_real_reply_time=?, hours_to_first_reply=?, last_modified_at=? "
            "WHERE request_id=?",
            (submission_time, first_auto_ack, first_real_reply, hours,
             last_modified, request_id),
        )
