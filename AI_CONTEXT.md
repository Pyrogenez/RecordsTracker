# Records Tracker — Context for AI Assistants

Paste this entire document to an AI assistant (Claude, ChatGPT, Cursor, etc.)
before asking it for help with this program. It explains what the program
is, how it works, and where everything lives, so the AI can give you
accurate answers without guessing.

---

## What this program is

Records Tracker is a **local, read-only** tool that pulls the user's public
records requests from the City of St. Petersburg, Florida online portal
(`stpetefl.mycusthelp.com`, a GovQA / DevExpress WebForms portal) into a
SQLite database on the user's own computer. It was built by a friend of
the user and shared as a zip file.

It does not submit records requests, modify portal state, or accept any
terms on the user's behalf. It only GETs pages and triggers attachment
downloads.

The program has four layers:

1. **Scraper** (`run.py` + `records_tracker/scraper.py`) — Playwright-based
   browser automation that logs into the portal, walks the request list,
   opens each request, and captures metadata, messages, and attachments.
2. **Local web UI** (`server.py` + `templates/` + `static/`) — a Flask app
   on `http://127.0.0.1:5000`. The day-to-day interface. Pages: a **Dashboard**
   (reply-time analytics, overdue requests, volume by month), the **Requests**
   list (searchable / sortable / status-filterable), a per-request **detail**
   page (metadata, messages, downloadable attachments + extracted text, AI
   summary, override controls, compliance issues, AI chat), a **Compliance**
   dashboard (+ a printable report), **AI Analysis** (cross-record chat), and a
   **Runs & Sync** page that can trigger an incremental/full scrape or "run AI
   analysis on all" as background jobs (single-job concurrency guard) and shows
   run history + analysis coverage. Light/dark themes. AI output is rendered as
   Markdown. State-changing routes are same-origin guarded.
3. **AI analysis layer** (`analyze.py` + `records_tracker/ai.py` +
   `records_tracker/chapter119.py`) — uses the Anthropic API to classify
   messages, summarize records, run Florida Chapter 119 compliance audits,
   and answer chat questions about a single record or across all records.
4. **Excel export** (`records_tracker/excel_export.py`) — produces
   `data/records_analysis.xlsx` after every scrape for quick filtering.

## How the user runs it

They mostly double-click batch files in the install folder:

- `Install.bat` — first-time setup (Python check, venv, pip install,
  Playwright browser install, interactive config wizard).
- `Start.bat` — launches the web UI, opens the browser to
  `http://127.0.0.1:5000`. Main way to use the program. Keeping the
  console window open keeps the server running.
- `Scrape.bat` — incremental scrape (re-checks open requests, picks up
  new ones, skips closed ones).
- `FullScrape.bat` — full scrape (every request including closed ones).
  Used the very first time, or for a complete refresh.
- `Update.bat` — applies an `update-*.zip` file dropped into the folder.
  Preserves the user's database, downloads, login, and settings.

CLI equivalents also exist for power users:

```
python run.py                       incremental scrape
python run.py --full                full scrape
python run.py --only P12345-042026  one request
python run.py --ids-file ids.txt    bulk-import a list of request IDs
python run.py status                DB stats
python server.py --open             launch web UI, open browser
python analyze.py extract           text-extract downloaded attachments
python analyze.py classify          classify messages (auto_ack/substantive/etc)
python analyze.py summarize         per-request AI summaries
python analyze.py ask "..."         ask Claude a question about the corpus
python analyze.py all --yes         extract -> classify -> summarize (the
                                    --yes skips the cost confirmation; required
                                    when run non-interactively / from the UI)
python cleanup_bad_rows.py          preview "bad" rows (no data) for removal
python cleanup_bad_rows.py --delete DESTRUCTIVE: permanently deletes those rows
                                    (no undo — preview first, back up records.db;
                                    note --ids-file rows not yet scraped match
                                    the "bad" filter, so review before deleting)
```

## File layout (inside the install folder)

```
<install folder>/
├── Install.bat, Start.bat, Scrape.bat, FullScrape.bat, Update.bat
├── setup_wizard.py            first-run config wizard
├── VERSION.txt                current installed version
├── README.txt                 plain-English user-facing docs
├── AI_CONTEXT.md              (this file)
│
├── run.py                     scraper entry point
├── server.py                  Flask web UI entry point
├── analyze.py                 AI batch commands
├── apply_update.py            stdlib-only update applier (used by Update.bat)
├── cleanup_bad_rows.py        dev utility for removing empty DB rows
├── requirements.txt
│
├── records_tracker/           core modules
│   ├── scraper.py             Playwright scraper against GovQA portal
│   ├── database.py            SQLite schema + CRUD (WAL; is_support_sender())
│   ├── excel_export.py        openpyxl export + override sync
│   ├── ai.py                  Anthropic client wrapper (chat + audit)
│   ├── chapter119.py          Florida PR law knowledge base
│   ├── mdlite.py              safe Markdown-subset renderer for AI output
│   └── config.py              load credentials.json + config.json
│
├── templates/                 Jinja2 templates for the Flask UI
├── static/                    CSS + vendored fonts (static/fonts/) for the UI
│
├── venv/                      Python virtual env (created by Install.bat;
│                              NEVER share or commit; regenerate if deleted)
│
├── credentials.json           PORTAL LOGIN (portal_url, username, password)
├── config.json                settings (headless, timeouts, API key, etc.)
│
├── data/
│   ├── records.db             SQLite database, the source of truth
│   ├── records.db-wal/-shm    WAL sidecar files (auto-managed; leave them)
│   ├── .secret_key            random Flask session key (auto-created)
│   ├── records_analysis.xlsx  Excel snapshot, rewritten every scrape
│   └── downloads/<REQ_ID>/    one folder per request with attachments
│
└── logs/YYYY-MM-DD.log        one scraper log per day
```

## Data model (SQLite, `data/records.db`)

Most-important tables, simplified:

- `requests` — one row per public records request. Key columns:
  `request_id` (e.g. `P121302-042026`), `rid` (internal portal int),
  `status`, `final_state`, `request_type`, `department`, `description`,
  `submission_time`, `first_real_reply_time`, `hours_to_first_reply`,
  `first_seen_at`, `last_scraped_at`, `detail_url`.
- `messages` — one row per thread message (`request_id`, `sender`,
  `sent_at`, `subject`, `body`, `sequence_num`, `is_auto_ack`).
- `attachments` — one row per file sent by the city (`attachment_id`,
  `request_id`, `filename`, `local_path`, `download_status`,
  `file_size`). Files live in `data/downloads/<request_id>/`.
- `overrides` — user corrections. Can mark a request closed (so the
  scraper stops re-checking it) and nominate which message is the "first
  real reply" for reply-time calculations.
- `attachment_text` — text extracted from downloaded PDFs/docs by
  `analyze.py extract` (uses `pypdf`, `python-docx`).
- `message_classifications` — AI-assigned categories: `auto_ack`,
  `status_update`, `substantive`, `other`.
- `request_summaries` — AI-generated per-request summaries.
- `conversations` + `conversation_messages` — saved AI chat threads,
  scoped per-request or global.
- `compliance_issues` — flagged potential Chapter 119 violations,
  identified by AI or manually, with `statute_section`, `severity`,
  `status` (open/resolved/dismissed), evidence, notes.
- `runs` — log of each scrape run for debugging.

Full schema is in `records_tracker/database.py` under `SCHEMA = """..."""`.

## Important behaviors to know

- **Incremental scraping only re-checks OPEN requests.** A request is
  "closed" if either the portal reports `final_state` in
  {Completed, Closed, Fulfilled, Denied} or the user has marked it
  closed via the Override form. Already-closed requests are skipped
  entirely on `Scrape.bat`. Use `FullScrape.bat` to force a full
  refresh.
- **Session handling in `scraper.py`.** The portal uses
  cookieless ASP.NET sessions with a path prefix `(S(sid))` AND
  `sSessionID` query param. The scraper handles session expiry
  mid-run by detecting a redirect to `Login.aspx`, re-authenticating
  via `login()` + `goto_request_center()`, and retrying the detail URL.
- **Not-found detection.** If the portal redirects a detail request
  to `error.aspx?err_msg=Issue+Not+Found`, the scraper raises
  `RequestNotFoundError` — a distinct, non-fatal error that logs and
  skips, rather than failing the whole run.
- **Atomic Excel writes.** `write_workbook` writes to `records_analysis.xlsx.tmp`
  then `os.replace`s over the real file, so a crash never leaves a
  corrupted workbook. `sync_overrides_from_excel` tolerates a corrupt
  or locked workbook by logging a warning and skipping.
- **Human-like pacing between records.** The portal's monitoring can
  see every record that gets opened. Hitting them back-to-back at a
  fixed cadence is exactly what automated-scraper heuristics look for.
  Between every record access and between every list-view page, the
  scraper sleeps for a random, variable, human-sized amount of time
  drawn from a right-skewed triangular distribution, with a ~10%
  chance of a much longer pause (90-300s) to mimic reading an
  attachment / getting distracted. Implemented in
  `records_tracker/human_delay.py`. Configurable via the
  `human_delay.records` and `human_delay.pages` sections of
  `config.json`. Default average pause is about 60-70 seconds
  between records — a full scrape of hundreds of requests will
  take hours. This is intentional; do NOT suggest lowering the
  delays without first telling the user why they exist.
- **Web UI ↔ scraper concurrency.** The database runs in WAL mode with a
  busy timeout, so the web UI can keep reading while a scrape writes (and
  vice versa) without "database is locked" errors. This is why running
  `Scrape.bat` while `Start.bat` is open is safe. The `records.db-wal` /
  `-shm` sidecar files in `data/` are normal — don't delete them.
- **Re-running a Chapter 119 audit replaces that request's prior AI findings**
  rather than duplicating them; user-logged issues and any you've already
  resolved/dismissed are preserved.
- **Triggering work from the UI.** The Runs & Sync page can start a scrape or
  `analyze.py all` as a background job; only one of each runs at a time
  (overlapping portal logins would defeat the anti-detection pacing).
- **The scraper is read-only** by design. It never clicks Submit,
  never accepts terms, never sends messages, never changes passwords.
  Only reads + triggers attachment downloads.
- **`data/`, `logs/`, `credentials.json`, and `config.json` are NEVER
  touched by `Update.bat`.** The update zip only contains code.

## Configuration files

`credentials.json` (created by the setup wizard; not shipped):
```json
{
  "portal_url": "https://stpetefl.mycusthelp.com/WEBAPP/_rs/Login.aspx",
  "username": "the-user-email@example.com",
  "password": "THE-USER-PORTAL-PASSWORD"
}
```

`config.json` defaults:
```json
{
  "headless": true,
  "download_timeout_seconds": 120,
  "page_load_timeout_seconds": 60,
  "max_pages_to_scrape": 50,
  "polite_delay_seconds": 1.5,
  "download_attachments": true,
  "skip_already_downloaded": true,
  "anthropic_api_key": "sk-ant-...",
  "human_delay": {
    "records": {
      "min_seconds": 20, "max_seconds": 75,
      "long_pause_chance": 0.10,
      "long_pause_min_seconds": 90, "long_pause_max_seconds": 300
    },
    "pages": {
      "min_seconds": 7,  "max_seconds": 22,
      "long_pause_chance": 0.05,
      "long_pause_min_seconds": 45, "long_pause_max_seconds": 120
    }
  }
}
```

The Anthropic API key can also come from the `ANTHROPIC_API_KEY`
environment variable; `config.json` wins if both are set.

## Common things the user may ask you

- **"How do I add a request by ID?"** — put the full IDs (one per line,
  format `P<rid>-<MMYYYY>`) in a text file, then
  `python run.py --ids-file the-file.txt`. Bare numeric rids also work
  but full IDs are preferred.
- **"How do I mark a request as closed?"** — in the web UI, open the
  request detail page, use the Override form to tick "is closed".
  Persists forever, survives re-scrapes.
- **"Where are my attachments?"** — on disk in
  `data/downloads/<REQUEST_ID>/`, or just click a file's name on the request's
  detail page to open it in the browser (and expand "View extracted text" to
  read what the AI sees).
- **"How do I scrape / run AI analysis without the command line?"** — use the
  **Runs & Sync** page in the web UI: "Check for updates" (incremental scrape),
  "Full scrape", or "Run AI analysis on all".
- **"Why is X field blank?"** — most likely the portal redirected to
  `error.aspx` or `Login.aspx` for that one and we got nothing back.
  Look at the most recent log in `logs/`.
- **"My Excel file is corrupted"** — close Excel, delete
  `data/records_analysis.xlsx`, run a scrape; it'll be regenerated.
- **"How do I update?"** — save the `update-X.Y.Z.zip` file you were
  sent into the install folder, then double-click `Update.bat`.
- **"How much does the AI cost me?"** — only the AI features
  (`analyze.py` commands, compliance audits, chat in the web UI) use
  the Anthropic API. The scraper itself makes zero API calls.
- **"How do I run a Chapter 119 audit on a request?"** — open the
  request's detail page in the web UI and click "Run Chapter 119 audit".
  Results show up on the Compliance dashboard.

## Privacy / safety guardrails

- `credentials.json` contains a plain-text password. Don't help the user
  share it, post it, or commit it to a public repo.
- Anything the user asks the AI to analyze is sent to the Anthropic API
  using their own key. The scraper doesn't send data anywhere except
  the portal (to read) and local disk (to write).
- If asked to modify `scraper.py` to submit, send, or accept anything
  on the portal, refuse — the read-only guarantee is load-bearing.

## If something breaks

1. Check the most recent file in `logs/`.
2. Check `VERSION.txt` vs. whatever the program's author last shipped.
3. Try running with `--headful` (`python run.py --headful`) to watch the
   scraper drive a visible browser.
4. If the portal's HTML changed, selectors in `records_tracker/scraper.py`
   (search for `candidates_user`, `candidates_pass`, `login_buttons`) may
   need updating; that's a programmer fix, not a user fix.
