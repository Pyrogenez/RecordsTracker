# Changelog

All notable changes to RecordsTracker. The web app shows an "Update now" banner
when a newer **release** is published here.

## 1.5.0
- **Full-text search** across every request, message, and the text inside
  downloaded attachments (SQLite FTS5 — no new dependency). New "Search" page.
- **Draft a letter with AI**: generate a polite follow-up email or a § 119.12
  pre-suit demand letter from a record's specific facts, delays, fees, and the
  statutes/cases in the reference — saved as a thread you can edit, copy, or print.
- **Safer updates**: the app now tracks published GitHub **releases** (deliberate,
  tagged versions) instead of the latest commit, and shows release notes
  ("What's new"). Set `update_check.channel` to `main` to track the branch tip.
- **Automated test suite + GitHub Actions CI** — a green check gates every push,
  protecting users from a bad auto-update.
- AI audit prompts tightened to cite **only** statutes/cases present in the
  reference (fewer hallucinated authorities).

## 1.4.0
- Automatic update notifications from GitHub + one-click self-update (your data is
  backed up first; dependencies refreshed).
- Data safety: verified database backups before every update, integrity/role-aware
  pruning, restore/revert (Backup/Restore launchers + the Runs page), a guard that
  refuses to restore while the app is open, and auto-rollback on a failed update.

## 1.3.0
- Request legibility: human labels + editable nicknames everywhere a request is
  shown or picked; log compliance issues directly from a record.
- AI cost controls (config-driven models, cached context) and macOS `.command`
  launchers for cross-platform use.

## 1.2.0
- A bold "civic dossier" UI redesign (sidebar, dashboard, light/dark themes,
  vendored fonts), in-UI scrape/analysis triggers, attachment viewing, a printable
  compliance report, markdown rendering, and requests search/sort/filter.
- Backend correctness, security (CSRF/same-origin guard, WAL), and robustness fixes.

## 1.1.0
- Initial shipped version: read-only GovQA scraper, SQLite storage, Flask web UI,
  Anthropic-powered Chapter 119 analysis, Excel export, and a Windows installer.
