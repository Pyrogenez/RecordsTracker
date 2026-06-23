"""Remove requests from the DB that look like they belong to another account.

A row is flagged as "bad" if ALL of the following are true:
    - status is NULL or empty
    - request_type is NULL or empty
    - it has zero messages
    - it has zero attachments

Those are rows that came in via --ids-file but the portal returned
"Issue Not Found" (error.aspx) or an empty detail page, so no real
data was ever captured. Real requests always have at least a status
or a request_type after a successful scrape.

Usage (from the project root):
    python cleanup_bad_rows.py           # preview only
    python cleanup_bad_rows.py --delete  # actually delete

Safe to run multiple times.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "records.db"

# A row is "bad" when the portal gave us nothing usable.
WHERE_BAD = """
    (status IS NULL OR TRIM(status) = '')
    AND (request_type IS NULL OR TRIM(request_type) = '')
    AND request_id NOT IN (SELECT DISTINCT request_id FROM messages)
    AND request_id NOT IN (SELECT DISTINCT request_id FROM attachments)
"""


def preview(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""
        SELECT request_id, rid, status, request_type, first_seen_at
        FROM requests
        WHERE {WHERE_BAD}
        ORDER BY rid
        """
    ).fetchall()
    return rows


def delete(conn: sqlite3.Connection) -> int:
    cur = conn.execute(f"DELETE FROM requests WHERE {WHERE_BAD}")
    conn.commit()
    return cur.rowcount


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--delete",
        action="store_true",
        help="Actually delete the rows (default is preview-only).",
    )
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB_PATH)
    try:
        rows = preview(conn)

        if not rows:
            print("No bad rows found. Database is clean.")
            return 0

        print(f"Found {len(rows)} row(s) with no usable data:")
        print()
        print(f"  {'request_id':<20} {'rid':>8}  {'status':<12} {'type':<20}  first_seen_at")
        print(f"  {'-'*20} {'-'*8}  {'-'*12} {'-'*20}  {'-'*20}")
        for r in rows:
            print(
                f"  {(r['request_id'] or ''):<20} "
                f"{(r['rid'] or 0):>8}  "
                f"{(r['status'] or ''):<12} "
                f"{(r['request_type'] or ''):<20}  "
                f"{(r['first_seen_at'] or '')}"
            )
        print()

        if not args.delete:
            print("Preview only. Re-run with --delete to remove these rows:")
            print("    python cleanup_bad_rows.py --delete")
            return 0

        n = delete(conn)
        print(f"Deleted {n} row(s) from the requests table.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
