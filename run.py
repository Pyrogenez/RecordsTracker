"""Main entry point — read-only scrape of the Public Records Center.

Typical workflow
----------------
FIRST TIME (one-time baseline — pulls everything that currently exists):
    python run.py --full

AFTERWARDS (scheduled 3x/day — efficient incremental update):
    python run.py

    Incremental mode walks the portal list newest-first and stops as soon
    as it sees a request already in the DB (everything past that is
    already recorded). It then also directly refreshes every request the
    DB still considers OPEN — those may be buried many pages deep past
    the stop point, so the listing walk alone would miss updates on them.

OTHER USEFUL COMMANDS:
    python run.py status         — print counts + last-run info, no network access
    python run.py --dry-run      — log in + scrape but don't write anything
    python run.py --headful      — watch the browser run (debug)
    python run.py --no-downloads — metadata only, skip file downloads
    python run.py --only P121302-042026  — just one request
    python run.py --ids-file ids.txt     — one-off import of a known ID list
                                           (skips listing walk; hits detail URLs directly)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import traceback
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from records_tracker import human_delay
from records_tracker.config import load_config, load_credentials, project_paths
from records_tracker.database import Database
from records_tracker.excel_export import sync_overrides_from_excel, write_workbook
from records_tracker.scraper import PortalScraper, RequestNotFoundError, RequestSummary


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logfile = log_dir / f"{datetime.now().strftime('%Y-%m-%d')}.log"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(logfile, encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="St. Pete Public Records tracker (read-only)")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("status", help="Print DB counts + last run info, then exit")

    p.add_argument("--full", action="store_true",
                   help="Scrape every request (including closed ones). Use for "
                        "the first-ever run; subsequent runs should omit this.")
    p.add_argument("--no-downloads", action="store_true",
                   help="Don't download attachments; metadata only.")
    p.add_argument("--headful", action="store_true",
                   help="Show the browser UI for debugging.")
    p.add_argument("--dry-run", action="store_true",
                   help="Don't write to DB or filesystem. Still logs in and scrapes.")
    p.add_argument("--only", metavar="REQUEST_ID",
                   help="Restrict to a single request ID (e.g. P121302-042026).")
    p.add_argument("--ids-file", metavar="PATH",
                   help="One-off import: scrape a list of request IDs from a "
                        "text file (one per line). Skips the portal listing "
                        "walk entirely — hits each request's detail page "
                        "directly. Full IDs 'P######-######' preferred; "
                        "rid-only numeric lines also accepted. Lines starting "
                        "with '#' and blank lines are ignored.")
    return p.parse_args()


def command_status(paths: dict[str, Path]) -> int:
    """Print a summary of DB contents without hitting the network."""
    if not paths["database"].exists():
        print("No database yet. Run `python run.py --full` first.")
        return 0
    db = Database(paths["database"])
    try:
        c = db.counts()
        last = db.get_last_run()
        print(f"Database:       {paths['database']}")
        print(f"Excel:          {paths['excel']}")
        print(f"Downloads:      {paths['downloads']}")
        print("")
        print(f"Total requests:            {c['total_requests']}")
        print(f"  Open (checked on runs):  {c['open_requests']}")
        print(f"  Closed by user:          {c['user_closed_requests']}")
        print(f"Total messages:            {c['total_messages']}")
        print(f"Total attachments:         {c['total_attachments']}")
        print(f"  Downloaded:              {c['downloaded_attachments']}")
        print(f"  Pending / failed:        {c['pending_attachments']}")
        print(f"  With extracted text:     {c['attachments_with_text']}")
        print(f"Classified messages (AI):  {c['classified_messages']}")
        print(f"Summarized requests (AI):  {c['summarized_requests']}")
        print(f"Baseline run complete:     {db.has_baseline_run()}")
        print("")
        if last:
            started = last.get("started_at") or "?"
            finished = last.get("finished_at") or "(unfinished)"
            err = last.get("error")
            mode = last.get("mode") or "?"
            print(f"Last run ({mode}):")
            print(f"  started:  {started}")
            print(f"  finished: {finished}")
            print(f"  scraped:  {last.get('requests_scraped', 0)}  "
                  f"skipped: {last.get('requests_skipped', 0)}  "
                  f"new_req: {last.get('new_requests', 0)}  "
                  f"new_msg: {last.get('new_messages', 0)}  "
                  f"new_att: {last.get('new_attachments', 0)}")
            if err:
                print(f"  error:    {err}")
        else:
            print("No runs recorded yet.")
    finally:
        db.close()
    return 0


def main() -> int:
    args = parse_args()
    paths = project_paths()
    setup_logging(paths["logs"])
    log = logging.getLogger("run")

    if args.command == "status":
        return command_status(paths)

    config = load_config()
    if args.headful:
        config = replace(config, headless=False)
    if args.no_downloads:
        config = replace(config, download_attachments=False)
    credentials = load_credentials()

    db = Database(paths["database"])

    # If this is the user's first ever run and they didn't pass --full, nudge them.
    if (not db.has_baseline_run() and not args.full
            and not args.only and not args.ids_file):
        log.warning("No baseline run found. Forcing --full mode for this first run.")
        args.full = True

    # Apply any user edits in the Excel overrides sheet to SQLite BEFORE scraping,
    # so force-close overrides take effect on this run.
    applied = sync_overrides_from_excel(db, paths["excel"])
    if applied:
        log.info("Applied %d override row(s) from Excel", applied)

    mode = "full" if args.full else "incremental"
    run_id = None if args.dry_run else db.start_run(mode=mode)
    requests_scraped = 0
    requests_skipped = 0
    new_requests = 0
    new_messages = 0
    new_attachments = 0
    error_msg: str | None = None

    try:
        with PortalScraper(credentials, config, paths["logs"]) as scraper:
            scraper.login()
            scraper.goto_request_center()

            summaries = _collect_summaries_to_scrape(scraper, db, args, log)
            log.info("Collected %d request(s) to scrape this run", len(summaries))

            requests_not_found = 0
            for s in summaries:
                log.info("Scraping %s (rid=%d, status=%s)", s.request_id, s.rid, s.status)
                try:
                    fields, messages, attachments = scraper.scrape_detail(s)
                except RequestNotFoundError as e:
                    requests_not_found += 1
                    log.warning(
                        "Not found: %s (rid=%d) — portal says %r. Skipping.",
                        s.request_id, s.rid, e.portal_message,
                    )
                    continue
                except Exception as e:  # noqa: BLE001
                    log.error("Failed to scrape %s: %s", s.request_id, e)
                    log.debug("%s", traceback.format_exc())
                    continue
                requests_scraped += 1

                if args.dry_run:
                    log.info(
                        "  dry-run: %d msgs, %d attachments", len(messages),
                        len(attachments),
                    )
                    continue

                with db.transaction():
                    is_new = db.upsert_request({
                        "request_id": s.request_id,
                        "rid": s.rid,
                        "status": fields.get("status") or s.status,
                        "final_state": s.final_state,
                        "request_type": fields.get("request_type"),
                        "category": fields.get("category"),
                        "department": fields.get("department"),
                        "records_type": fields.get("records_type"),
                        "description": fields.get("description"),
                        "preferred_method": fields.get("preferred_method"),
                        "requester_email": fields.get("requester_email"),
                        "detail_url": s.detail_url,
                    })
                    if is_new:
                        new_requests += 1
                    for m in messages:
                        if db.upsert_message({
                            "message_id": m.message_id,
                            "request_id": s.request_id,
                            "sent_at": m.sent_at,
                            "sender": m.sender,
                            "subject": m.subject,
                            "body": m.body,
                            "sequence_num": m.sequence_num,
                            "is_auto_ack": 1 if m.is_auto_ack else 0,
                        }):
                            new_messages += 1
                    for a in attachments:
                        if db.upsert_attachment({
                            "attachment_id": a.attachment_id,
                            "request_id": s.request_id,
                            "rid": s.rid,
                            "filename": a.filename,
                            "local_path": None,
                            "download_status": "pending",
                            "file_size": None,
                            "downloaded_at": None,
                            "error_message": None,
                        }):
                            new_attachments += 1
                    db.recompute_first_reply(s.request_id)

                if config.download_attachments and not args.dry_run:
                    _download_pending_for_request(scraper, db, paths["downloads"],
                                                  s.rid, s.request_id, log)

                # Human-like pause between records. The portal can see every
                # record we open; hitting them back-to-back is what trips
                # "automated scraper" heuristics. Sleep a random, variable,
                # realistic amount of time — as if the user just finished
                # reading this record and is deciding what to click next.
                # Configurable via the `human_delay.records` section of
                # config.json. See records_tracker/human_delay.py.
                human_delay.sleep_between_records(config.human_delay_records)

    except Exception as e:  # noqa: BLE001
        error_msg = f"{type(e).__name__}: {e}"
        log.exception("Run failed")

    finally:
        if run_id is not None:
            db.finish_run(
                run_id,
                requests_scraped=requests_scraped,
                requests_skipped=requests_skipped,
                new_requests=new_requests,
                new_messages=new_messages,
                new_attachments=new_attachments,
                error=error_msg,
            )
        if not args.dry_run:
            try:
                write_workbook(db, paths["excel"])
                log.info("Excel workbook written to %s", paths["excel"])
            except Exception:
                log.exception("Failed to write Excel workbook")
        db.close()

    log.info(
        "Done. mode=%s scraped=%d skipped=%d not_found=%d "
        "new_req=%d new_msg=%d new_att=%d",
        mode, requests_scraped, requests_skipped,
        locals().get("requests_not_found", 0),
        new_requests, new_messages, new_attachments,
    )
    return 0 if error_msg is None else 1


def _collect_summaries_to_scrape(
    scraper: PortalScraper,
    db: Database,
    args: argparse.Namespace,
    log: logging.Logger,
) -> list[RequestSummary]:
    """Build the list of request summaries to detail-scrape this run.

    Full run / --only
    -----------------
    Walk every listing page, yield everything. (Closed-in-DB requests get
    refreshed too — user may have intentionally forced a re-scrape.)

    Incremental run
    ---------------
    Two disjoint sources, union'd:
      1. NEW requests on the portal that we haven't logged yet. We walk the
         list newest-first and stop as soon as we hit a request already in
         the DB — everything past that is already recorded.
      2. Requests the DB still considers OPEN (no Completed/Closed/Denied
         final_state, not user-closed via override). These may be buried
         many pages deep; we fetch each one's detail page directly.

    This avoids walking all ~30 pages just to filter most out.
    """
    if args.ids_file:
        return _summaries_from_ids_file(scraper, Path(args.ids_file), log)

    if args.full or args.only:
        summaries = list(scraper.iter_all_request_summaries())
        log.info("Found %d requests across all pages", len(summaries))
        if args.only:
            summaries = [s for s in summaries if s.request_id == args.only]
            log.info("Filtered to %d request(s) matching --only", len(summaries))
        return summaries

    # ---- incremental -------------------------------------------------------
    def is_known(s: RequestSummary) -> bool:
        return db.request_exists(s.request_id)

    new_summaries = list(scraper.iter_new_request_summaries(is_known))
    log.info("Listing walk: %d new request(s) on portal", len(new_summaries))

    yielded_ids = {s.request_id for s in new_summaries}
    open_rows = db.get_open_requests()
    refresh: list[RequestSummary] = []
    for row in open_rows:
        if row["request_id"] in yielded_ids:
            # Already included via the listing walk — no need to dedupe-fetch.
            continue
        rid = int(row["rid"])
        refresh.append(RequestSummary(
            request_id=row["request_id"],
            rid=rid,
            status=row.get("status") or "",
            final_state=row.get("final_state"),
            # Always rebuild the URL from the current session path. The URL
            # stored in DB may carry a stale `/(S(sid))/` prefix that the
            # portal no longer honors after re-login.
            detail_url=scraper._detail_url_for(rid),  # noqa: SLF001
        ))
    log.info(
        "Open-in-DB refresh: %d request(s) still open to recheck (of %d open total)",
        len(refresh), len(open_rows),
    )
    return new_summaries + refresh


def _summaries_from_ids_file(
    scraper: PortalScraper,
    path: Path,
    log: logging.Logger,
) -> list[RequestSummary]:
    """Parse a newline-delimited text file of request IDs and build synthetic
    RequestSummary objects that navigate straight to each detail page.

    Supported line formats (one per line):
      * Full ID:   P121302-042026
      * rid only:  121302           (uses 'P<rid>' as placeholder DB key)

    Blank lines and lines starting with '#' are ignored.
    """
    import re

    FULL_RE = re.compile(r"^(P\d+-\d+)$", re.IGNORECASE)
    RID_RE = re.compile(r"^(\d+)$")

    if not path.exists():
        log.error("IDs file not found: %s", path)
        return []

    summaries: list[RequestSummary] = []
    rid_only_count = 0
    bad: list[str] = []
    seen_rids: set[int] = set()

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        m = FULL_RE.match(line)
        if m:
            request_id = m.group(1).upper()
            rid = int(request_id.split("-", 1)[0][1:])  # strip 'P', drop '-MMDDYY'
        else:
            m = RID_RE.match(line)
            if m:
                rid = int(m.group(1))
                request_id = f"P{rid}"  # placeholder; listing walk can later supply full ID
                rid_only_count += 1
            else:
                bad.append(line)
                continue

        if rid in seen_rids:
            continue
        seen_rids.add(rid)

        summaries.append(RequestSummary(
            request_id=request_id,
            rid=rid,
            status="",
            final_state=None,
            detail_url=scraper._detail_url_for(rid),  # noqa: SLF001
        ))

    if bad:
        log.warning(
            "Skipped %d unparseable line(s) in %s; first offender: %r",
            len(bad), path, bad[0],
        )
    if rid_only_count:
        log.warning(
            "%d line(s) were rid-only (numeric). They were imported with a "
            "placeholder request_id of 'P<rid>'. Re-running `python run.py "
            "--full` later will upsert the canonical 'P<rid>-MMDDYY' key from "
            "the listing page if those requests appear there.",
            rid_only_count,
        )
    log.info("Loaded %d unique request ID(s) from %s", len(summaries), path)
    return summaries


def _download_pending_for_request(scraper, db: Database, downloads_root: Path,
                                  rid: int, request_id: str, log) -> None:
    pending = db.get_pending_attachments(request_id=request_id)
    if not pending:
        return
    dest_dir = downloads_root / request_id
    # We need the live attachment list from the page because postback targets
    # are tied to the current page render order.
    live = scraper._extract_attachments()  # noqa: SLF001  (internal helper reuse)
    by_id = {a.attachment_id: a for a in live}
    for row in pending:
        att = by_id.get(row["attachment_id"])
        if att is None:
            log.warning("Attachment %s not found on current page", row["attachment_id"])
            continue
        log.info("  downloading %s (%d)", att.filename, att.attachment_id)
        try:
            dest = scraper.download_attachment(rid, att, dest_dir)
            size = dest.stat().st_size
            db.mark_attachment_downloaded(att.attachment_id, str(dest), size)
        except Exception as e:  # noqa: BLE001
            log.error("    failed: %s", e)
            db.mark_attachment_failed(att.attachment_id, str(e))


if __name__ == "__main__":
    raise SystemExit(main())
