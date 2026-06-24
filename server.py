"""Local web UI for the Public Records tracker.

Launches a Flask server on 127.0.0.1 so the user can browse requests, chat with
Claude about a specific record, run a Chapter 119 compliance audit, review
flagged issues, view a reply-time dashboard, open downloaded attachments, and
kick off scrapes / AI analysis — all locally.

Usage:
    python server.py               # serves on http://127.0.0.1:5000
    python server.py --port 8765
    python server.py --open        # opens browser automatically
"""
from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import secrets
import statistics
import subprocess
import sys
import threading
import webbrowser
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template, request,
    send_file, session, url_for,
)

from records_tracker import ai as ai_mod
from records_tracker import backup as backup_mod
from records_tracker import updater
from records_tracker.config import PROJECT_ROOT, project_paths
from records_tracker.database import Database, is_support_sender, request_label
from records_tracker.mdlite import looks_like_json, markdown_to_html

log = logging.getLogger("server")

# Open requests with no substantive reply older than this are "overdue" on the
# dashboard. Not a legal deadline — just a reviewer's at-a-glance threshold.
OVERDUE_DAYS = 30

# Tracks background scrape/analyze subprocesses started from the UI, so we never
# launch overlapping portal sessions (which is exactly the bot signature the
# human-delay pacing exists to avoid). Guarded by _ACTIONS_LOCK.
_ACTIONS: dict[str, subprocess.Popen] = {}
_ACTIONS_LOCK = threading.Lock()


def _audit_assessment(db: Database, request_id: str) -> dict | None:
    """Pull the latest saved Chapter 119 audit's overall_assessment +
    recommended_actions out of its conversation transcript (where they're
    otherwise buried as raw JSON)."""
    title = f"Compliance audit — {request_id}"
    convs = [c for c in db.list_conversations(scope="request", request_id=request_id)
             if c["title"] == title]
    if not convs:
        return None
    msgs = db.get_conversation_messages(convs[0]["conversation_id"])
    assistant = [m for m in msgs if m["role"] == "assistant"]
    if not assistant:
        return None
    parsed = ai_mod._parse_json_lenient(assistant[-1]["content"])  # noqa: SLF001
    if not parsed:
        return None
    return {
        "overall_assessment": parsed.get("overall_assessment"),
        "recommended_actions": parsed.get("recommended_actions") or [],
    }


def _app_version() -> str:
    try:
        return (PROJECT_ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return "?"


def _get_secret_key(data_dir: Path) -> str:
    """Random, persisted secret key kept in the user-data dir (never shipped,
    survives code-only updates) so sessions/flash stay valid across restarts."""
    key_path = data_dir / ".secret_key"
    try:
        if key_path.exists():
            return key_path.read_text(encoding="utf-8").strip()
        key = secrets.token_urlsafe(48)
        data_dir.mkdir(parents=True, exist_ok=True)
        key_path.write_text(key, encoding="utf-8")
        return key
    except OSError:
        # Fall back to an ephemeral key if the data dir isn't writable.
        return secrets.token_urlsafe(48)


def _action_running(name: str) -> bool:
    with _ACTIONS_LOCK:
        proc = _ACTIONS.get(name)
        return proc is not None and proc.poll() is None


def _start_action(name: str, argv: list[str],
                  log_path: str | None = None) -> tuple[bool, str]:
    """Start a background subprocess for `name` unless one is already running.
    Returns (started, message). If log_path is given, the child's output goes
    there (so failures are diagnosable instead of swallowed)."""
    with _ACTIONS_LOCK:
        existing = _ACTIONS.get(name)
        if existing is not None and existing.poll() is None:
            return False, f"A {name} is already running. Let it finish first."
        try:
            out = open(log_path, "w", encoding="utf-8") if log_path else subprocess.DEVNULL
            proc = subprocess.Popen(  # noqa: S603
                [sys.executable, *argv],
                cwd=str(PROJECT_ROOT),
                stdout=out,
                stderr=subprocess.STDOUT,
            )
        except Exception as e:  # noqa: BLE001
            return False, f"Could not start {name}: {e}"
        _ACTIONS[name] = proc
    return True, f"{name.capitalize()} started — watch progress on the Runs page."


def create_app() -> Flask:
    paths = project_paths()
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.secret_key = _get_secret_key(paths["data"])
    app.config.update(
        DB_PATH=paths["database"],
        DOWNLOADS_DIR=paths["downloads"],
        EXCEL_PATH=paths["excel"],
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_HTTPONLY=True,
    )

    # Apply schema + migrations ONCE at startup rather than on every request.
    Database(app.config["DB_PATH"], ensure_schema=True).close()

    # Refresh the "is there a newer version?" cache in the background so it never
    # slows a page load. Best-effort; silently does nothing if offline/private.
    threading.Thread(target=lambda: updater.check(force=False), daemon=True).start()

    # ----- DB per-request (schema already ensured at startup) -----
    def get_db() -> Database:
        if "db" not in g:
            g.db = Database(app.config["DB_PATH"], ensure_schema=False)
        return g.db

    @app.teardown_appcontext
    def close_db(exception=None):  # noqa: ARG001
        db = g.pop("db", None)
        if db is not None:
            db.close()

    # ----- CSRF / same-origin guard on state-changing requests -----
    # The server is reachable from JavaScript in any other browser tab, so a
    # malicious page could auto-submit cross-origin forms to delete data or burn
    # API budget. Reject any mutating request whose Origin/Referer is present and
    # not same-origin. (Normal browser form posts always carry one of them.)
    @app.before_request
    def _same_origin_guard():
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return None
        for header in ("Origin", "Referer"):
            value = request.headers.get(header)
            if value:
                if urlparse(value).netloc != request.host:
                    log.warning("Blocked cross-origin %s to %s (%s=%s)",
                                request.method, request.path, header, value)
                    abort(403)
                return None
        return None

    # ----- filters -----
    @app.template_filter("fmt_dt")
    def fmt_dt(value):
        if not value:
            return ""
        try:
            if "T" in str(value):
                dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                return dt.strftime("%Y-%m-%d %H:%M")
            return str(value)
        except Exception:
            return str(value)

    @app.template_filter("fmt_hours")
    def fmt_hours(value):
        if value is None:
            return "—"
        try:
            h = float(value)
            if h < 24:
                return f"{h:.1f} h"
            days = h / 24.0
            return f"{days:.1f} d ({h:.0f} h)"
        except Exception:
            return str(value)

    @app.template_filter("severity_class")
    def severity_class(value):
        v = (value or "").lower()
        return {"high": "sev-high", "medium": "sev-med", "low": "sev-low"}.get(v, "")

    @app.template_filter("md")
    def md(value):
        return markdown_to_html(value)

    @app.template_filter("mode_label")
    def mode_label(value):
        # Display label for a run mode; the stored value stays incremental/full.
        return {"incremental": "Quick check", "full": "Full re-check"}.get(
            (value or "").lower(), value or "?")

    app.jinja_env.filters["looks_like_json"] = looks_like_json
    app.jinja_env.filters["request_label"] = request_label

    # ----- shared template context -----
    app.config["APP_VERSION"] = _app_version()

    @app.context_processor
    def inject_globals():
        try:
            open_issues = get_db().open_compliance_count()
        except Exception:
            open_issues = 0
        return {
            "ai_enabled": bool(ai_mod.get_api_key()),
            "active_endpoint": request.endpoint,
            "scrape_running": _action_running("scrape"),
            "analyze_running": _action_running("analysis"),
            "update_running": _action_running("update"),
            "open_issue_count": open_issues,
            "app_version": app.config.get("APP_VERSION", "?"),
            "update_info": updater.cached(),  # cache only — never a network call here
        }

    # ===== routes =====

    @app.route("/")
    def index():
        db = get_db()
        requests_ = db.get_all_requests()
        counts = db.counts()
        last = db.get_last_run()
        status_filter = request.args.get("status") or "all"

        closed_overrides = {o["request_id"] for o in db.get_all_overrides()
                            if o.get("is_closed")}
        open_issues_by_req: dict[str, int] = defaultdict(int)
        for iss in db.get_compliance_issues(status="open"):
            open_issues_by_req[iss["request_id"]] += 1

        for r in requests_:
            r["_is_closed"] = db.is_request_closed(
                r["request_id"], r.get("final_state"),
                override_closed=r["request_id"] in closed_overrides)
            r["_open_issues"] = open_issues_by_req.get(r["request_id"], 0)

        if status_filter == "open":
            shown = [r for r in requests_ if not r["_is_closed"]]
        elif status_filter == "closed":
            shown = [r for r in requests_ if r["_is_closed"]]
        elif status_filter == "issues":
            shown = [r for r in requests_ if r["_open_issues"]]
        else:
            status_filter = "all"
            shown = requests_

        return render_template(
            "index.html", requests=shown, total_count=len(requests_),
            counts=counts, last_run=last, status_filter=status_filter,
        )

    @app.route("/requests/<request_id>")
    def request_detail(request_id: str):
        db = get_db()
        r = db.get_request(request_id)
        if not r:
            abort(404)
        messages = db.get_messages_for_request(request_id)
        for m in messages:
            cls = db.get_message_classification(m["message_id"])
            m["_classification"] = cls["classification"] if cls else None
            m["_is_support"] = is_support_sender(m["sender"])
        attachments = db.get_attachments_for_request(request_id)
        for a in attachments:
            txt = db.get_attachment_text(a["attachment_id"])
            a["_text"] = txt["extracted_text"] if txt and txt.get("extracted_text") else None
        override = db.get_override(request_id)
        summary = db.get_request_summary(request_id)
        issues = db.get_compliance_issues(request_id=request_id)
        conversations = db.list_conversations(scope="request", request_id=request_id)
        is_closed = db.is_request_closed(request_id, r.get("final_state"))
        assessment = _audit_assessment(db, request_id)
        # Carry the dashboard's "overdue" context onto the record itself: open,
        # no substantive reply, submitted more than OVERDUE_DAYS ago.
        days_open = None
        if not is_closed and not r.get("first_real_reply_time") and r.get("submission_time"):
            try:
                dt = datetime.fromisoformat(str(r["submission_time"]).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                days_open = (datetime.now(timezone.utc) - dt).days
            except Exception:
                days_open = None
        overdue = days_open is not None and days_open >= OVERDUE_DAYS
        return render_template(
            "request_detail.html", r=r, is_closed=is_closed, messages=messages,
            attachments=attachments, override=override, summary=summary,
            issues=issues, conversations=conversations, assessment=assessment,
            days_open=days_open, overdue=overdue,
        )

    @app.route("/requests/<request_id>/audit", methods=["POST"])
    def request_audit(request_id: str):
        db = get_db()
        anchor = ""
        try:
            result = ai_mod.audit_request_compliance(db, request_id)
            n = len(result.get("issues") or [])
            flash(f"Audit complete — {n} issue(s) found.", "ok")
            if n:
                anchor = "#issues"  # jump straight to the findings
        except ai_mod.AIConfigError as e:
            flash(str(e), "err")
        except ai_mod.AIResponseError as e:
            flash(str(e), "warn")
        except Exception:  # noqa: BLE001
            log.exception("audit failed")
            flash("The audit didn't go through. Check your internet connection and "
                  "that your Anthropic API key in config.json is valid, then try again.",
                  "err")
        return redirect(url_for("request_detail", request_id=request_id) + anchor)

    @app.route("/requests/<request_id>/summarize", methods=["POST"])
    def request_summarize(request_id: str):
        db = get_db()
        try:
            ai_mod.summarize_request(db, request_id)
            flash("Summary refreshed.", "ok")
        except ai_mod.AIConfigError as e:
            flash(str(e), "err")
        except Exception:  # noqa: BLE001
            log.exception("summarize failed")
            flash("Couldn't generate the summary. Check your internet connection and "
                  "your Anthropic API key in config.json, then try again.", "err")
        return redirect(url_for("request_detail", request_id=request_id))

    @app.route("/requests/<request_id>/title", methods=["POST"])
    def request_set_title(request_id: str):
        db = get_db()
        if not db.get_request(request_id):
            abort(404)
        db.set_short_title(request_id, request.form.get("short_title"))
        flash("Nickname saved.", "ok")
        return redirect(url_for("request_detail", request_id=request_id))

    @app.route("/requests/<request_id>/override", methods=["POST"])
    def request_override(request_id: str):
        db = get_db()
        if not db.get_request(request_id):
            abort(404)
        is_closed = bool(request.form.get("is_closed"))
        notes = (request.form.get("notes") or "").strip() or None
        msg_id_raw = (request.form.get("first_real_reply_message_id") or "").strip()
        msg_id: int | None = None
        if msg_id_raw:
            try:
                msg_id = int(msg_id_raw)
            except ValueError:
                flash("First-real-reply message ID must be a number.", "err")
                return redirect(url_for("request_detail", request_id=request_id))
            valid = {m["message_id"] for m in db.get_messages_for_request(request_id)}
            if msg_id not in valid:
                flash("That message ID isn't part of this request.", "err")
                return redirect(url_for("request_detail", request_id=request_id))
        db.upsert_override(request_id, msg_id, notes, is_closed=is_closed)
        with db.transaction():
            db.recompute_first_reply(request_id)
        flash("Override saved.", "ok")
        return redirect(url_for("request_detail", request_id=request_id))

    @app.route("/requests/<request_id>/first-reply", methods=["POST"])
    def request_set_first_reply(request_id: str):
        """Quick one-click 'this message is the first real reply' from the thread.
        Preserves the existing is_closed / notes override fields."""
        db = get_db()
        if not db.get_request(request_id):
            abort(404)
        try:
            msg_id = int(request.form.get("message_id") or "")
        except ValueError:
            flash("Invalid message.", "err")
            return redirect(url_for("request_detail", request_id=request_id))
        valid = {m["message_id"] for m in db.get_messages_for_request(request_id)}
        if msg_id not in valid:
            flash("That message isn't part of this request.", "err")
            return redirect(url_for("request_detail", request_id=request_id))
        existing = db.get_override(request_id) or {}
        db.upsert_override(
            request_id, msg_id, existing.get("notes"),
            is_closed=bool(existing.get("is_closed")))
        with db.transaction():
            db.recompute_first_reply(request_id)
        flash("First real reply set.", "ok")
        return redirect(url_for("request_detail", request_id=request_id))

    @app.route("/requests/<request_id>/retry-downloads", methods=["POST"])
    def request_retry_downloads(request_id: str):
        db = get_db()
        n = db.reset_failed_downloads(request_id)
        flash(f"Reset {n} failed download(s) — they'll be retried on the next scrape.",
              "ok" if n else "warn")
        return redirect(url_for("request_detail", request_id=request_id))

    # ---- attachments ----
    @app.route("/attachments/<int:attachment_id>/file")
    def attachment_file(attachment_id: int):
        db = get_db()
        a = db.get_attachment(attachment_id)
        if not a or a.get("download_status") != "downloaded" or not a.get("local_path"):
            abort(404)
        downloads = Path(app.config["DOWNLOADS_DIR"]).resolve()
        target = Path(a["local_path"]).resolve()
        try:  # containment: never serve anything outside data/downloads/
            target.relative_to(downloads)
        except ValueError:
            abort(403)
        if not target.exists():
            abort(404)
        return send_file(str(target), as_attachment=False,
                         download_name=a.get("filename") or target.name)

    # ---- conversations ----
    @app.route("/conversations/new", methods=["POST"])
    def conversation_new():
        db = get_db()
        if not ai_mod.get_api_key():
            flash("Add an Anthropic API key (in config.json) to use AI chat.", "err")
            return redirect(request.referrer or url_for("index"))
        scope = request.form.get("scope") or "global"
        request_id = request.form.get("request_id") or None
        title = (request.form.get("title") or "").strip()
        if not title:
            title = f"{'Cross-record' if scope == 'global' else request_id} — {datetime.now():%Y-%m-%d %H:%M}"
        conv_id = db.create_conversation(scope, title, request_id=request_id)
        return redirect(url_for("conversation_view", conversation_id=conv_id))

    @app.route("/conversations/<int:conversation_id>")
    def conversation_view(conversation_id: int):
        db = get_db()
        conv = db.get_conversation(conversation_id)
        if not conv:
            abort(404)
        messages = db.get_conversation_messages(conversation_id)
        return render_template("conversation.html", conv=conv, messages=messages)

    @app.route("/conversations/<int:conversation_id>/send", methods=["POST"])
    def conversation_send(conversation_id: int):
        db = get_db()
        user_text = (request.form.get("message") or "").strip()
        if not user_text:
            flash("Message cannot be empty.", "err")
            return redirect(url_for("conversation_view", conversation_id=conversation_id))
        try:
            ai_mod.continue_conversation(db, conversation_id, user_text)
        except ai_mod.AIConfigError as e:
            flash(str(e), "err")
        except Exception:  # noqa: BLE001
            log.exception("chat failed")
            flash("The AI reply didn't go through. Check your internet connection and "
                  "your Anthropic API key in config.json, then try again. "
                  "Your message wasn't sent — you can resend it.", "err")
        return redirect(url_for("conversation_view", conversation_id=conversation_id))

    @app.route("/conversations/<int:conversation_id>/rename", methods=["POST"])
    def conversation_rename(conversation_id: int):
        db = get_db()
        conv = db.get_conversation(conversation_id)
        if not conv:
            abort(404)
        title = (request.form.get("title") or "").strip()
        if title:
            db.rename_conversation(conversation_id, title)
            flash("Conversation renamed.", "ok")
        return redirect(url_for("conversation_view", conversation_id=conversation_id))

    @app.route("/conversations/<int:conversation_id>/delete", methods=["POST"])
    def conversation_delete(conversation_id: int):
        db = get_db()
        conv = db.get_conversation(conversation_id)
        if not conv:
            abort(404)
        db.delete_conversation(conversation_id)
        flash("Conversation deleted.", "ok")
        if conv["scope"] == "request" and conv.get("request_id"):
            return redirect(url_for("request_detail", request_id=conv["request_id"]))
        return redirect(url_for("global_analysis"))

    @app.route("/analysis")
    def global_analysis():
        db = get_db()
        conversations = db.list_conversations(scope="global")
        return render_template("global_analysis.html", conversations=conversations)

    # ---- compliance ----
    @app.route("/compliance")
    def compliance_dashboard():
        db = get_db()
        status_filter = request.args.get("status") or "open"
        if status_filter == "all":
            issues = db.get_compliance_issues()
        else:
            issues = db.get_compliance_issues(status=status_filter)
        by_request: dict[str, list[dict]] = {}
        for iss in issues:
            by_request.setdefault(iss["request_id"], []).append(iss)
        all_requests = db.get_all_requests()
        reqs = {r["request_id"]: r for r in all_requests}
        return render_template(
            "compliance.html", issues=issues, by_request=by_request,
            status_filter=status_filter, all_requests=all_requests, reqs=reqs,
        )

    @app.route("/compliance/report")
    def compliance_report():
        db = get_db()
        request_id = request.args.get("request_id") or None
        issues = db.get_compliance_issues(request_id=request_id)
        issues = [i for i in issues if i["status"] != "dismissed"]
        by_request: dict[str, list[dict]] = {}
        for iss in issues:
            by_request.setdefault(iss["request_id"], []).append(iss)
        reqs = {}
        assessments = {}
        for rid in by_request:
            reqs[rid] = db.get_request(rid)
            assessments[rid] = _audit_assessment(db, rid)
        return render_template(
            "compliance_report.html", by_request=by_request, reqs=reqs,
            assessments=assessments, request_id=request_id,
            generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )

    @app.route("/compliance/<int:issue_id>/update", methods=["POST"])
    def compliance_update(issue_id: int):
        db = get_db()
        status = request.form.get("status")
        user_notes = request.form.get("user_notes")
        severity = request.form.get("severity")
        updates: dict = {}
        if status and status in ("open", "resolved", "dismissed"):
            updates["status"] = status
        if user_notes is not None:
            updates["user_notes"] = user_notes.strip() or None
        if severity and severity in ("low", "medium", "high"):
            updates["severity"] = severity
        if updates:
            db.update_compliance_issue(issue_id, **updates)
            flash("Issue updated.", "ok")
        return redirect(request.referrer or url_for("compliance_dashboard"))

    @app.route("/compliance/<int:issue_id>/delete", methods=["POST"])
    def compliance_delete(issue_id: int):
        db = get_db()
        db.delete_compliance_issue(issue_id)
        flash("Issue deleted.", "ok")
        return redirect(request.referrer or url_for("compliance_dashboard"))

    @app.route("/compliance/new", methods=["POST"])
    def compliance_new():
        db = get_db()
        request_id = request.form.get("request_id")
        description = request.form.get("description") or ""
        if not request_id or not description.strip():
            flash("Request and description are required.", "err")
            return redirect(request.referrer or url_for("compliance_dashboard"))
        if not db.get_request(request_id):
            flash(f"No such request: {request_id}", "err")
            return redirect(request.referrer or url_for("compliance_dashboard"))
        db.add_compliance_issue({
            "request_id": request_id,
            "statute_section": request.form.get("statute_section") or None,
            "issue_type": request.form.get("issue_type") or "manual",
            "severity": request.form.get("severity") or "medium",
            "description": description,
            "evidence": request.form.get("evidence") or None,
            "identified_by": "user",
            "status": "open",
        })
        flash("Issue logged.", "ok")
        return redirect(request.referrer or url_for("compliance_dashboard"))

    # ---- dashboard (reply-time analytics) ----
    @app.route("/dashboard")
    def dashboard():
        db = get_db()
        reqs = db.get_all_requests()
        counts = db.counts()
        closed_overrides = {o["request_id"] for o in db.get_all_overrides()
                            if o.get("is_closed")}
        now = datetime.now(timezone.utc)

        reply_hours = [r["hours_to_first_reply"] for r in reqs
                       if r.get("hours_to_first_reply") is not None]
        avg_h = round(sum(reply_hours) / len(reply_hours), 1) if reply_hours else None
        med_h = round(statistics.median(reply_hours), 1) if reply_hours else None

        by_dept: dict[str, list[float]] = defaultdict(list)
        for r in reqs:
            if r.get("hours_to_first_reply") is not None:
                by_dept[r.get("department") or "—"].append(r["hours_to_first_reply"])
        dept_rows = sorted(
            ({"dept": d, "n": len(v), "avg": round(sum(v) / len(v), 1)}
             for d, v in by_dept.items()),
            key=lambda x: x["avg"], reverse=True,
        )

        overdue = []
        for r in reqs:
            is_closed = db.is_request_closed(
                r["request_id"], r.get("final_state"),
                override_closed=r["request_id"] in closed_overrides)
            if is_closed or r.get("first_real_reply_time"):
                continue
            sub = r.get("submission_time")
            if not sub:
                continue
            try:
                dt = datetime.fromisoformat(str(sub).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                days = (now - dt).days
            except Exception:
                continue
            if days >= OVERDUE_DAYS:
                overdue.append({"request_id": r["request_id"], "days": days,
                                "department": r.get("department") or "—"})
        overdue.sort(key=lambda x: x["days"], reverse=True)

        by_month: dict[str, int] = defaultdict(int)
        for r in reqs:
            sub = r.get("submission_time")
            if sub and len(str(sub)) >= 7:
                by_month[str(sub)[:7]] += 1
        month_rows = [{"month": m, "n": by_month[m]} for m in sorted(by_month)]

        max_dept = max((d["avg"] for d in dept_rows), default=1) or 1
        max_month = max((m["n"] for m in month_rows), default=1) or 1
        return render_template(
            "dashboard.html", counts=counts, avg_h=avg_h, med_h=med_h,
            sample=len(reply_hours), dept_rows=dept_rows, overdue=overdue,
            month_rows=month_rows, max_dept=max_dept, max_month=max_month,
            overdue_days=OVERDUE_DAYS,
        )

    # ---- runs / coverage / actions ----
    @app.route("/runs")
    def runs():
        db = get_db()
        recent = db.get_recent_runs(limit=30)
        counts = db.counts()
        coverage = {
            "messages_total": counts["total_messages"],
            "messages_classified": counts["classified_messages"],
            "attachments_downloaded": counts["downloaded_attachments"],
            "attachments_with_text": counts["attachments_with_text"],
            "requests_total": counts["total_requests"],
            "requests_summarized": counts["summarized_requests"],
        }
        update_result = None
        try:
            update_result = json.loads(
                (project_paths()["data"] / ".update_result.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        return render_template("runs.html", recent=recent, coverage=coverage,
                               counts=counts, has_baseline=db.has_baseline_run(),
                               backups=backup_mod.list_backups(), update_result=update_result)

    @app.route("/actions/scrape", methods=["POST"])
    def action_scrape():
        if _action_running("update"):
            flash("An update is in progress — wait for it to finish.", "warn")
            return redirect(url_for("runs"))
        mode = request.form.get("mode") or "incremental"
        argv = ["run.py"] + (["--full"] if mode == "full" else [])
        ok, msg = _start_action("scrape", argv)
        flash(msg, "ok" if ok else "warn")
        return redirect(url_for("runs"))

    @app.route("/actions/analyze", methods=["POST"])
    def action_analyze():
        if _action_running("update"):
            flash("An update is in progress — wait for it to finish.", "warn")
            return redirect(url_for("runs"))
        if not ai_mod.get_api_key():
            flash("Add an Anthropic API key (in config.json) to run AI analysis.", "err")
            return redirect(url_for("runs"))
        ok, msg = _start_action("analysis", ["analyze.py", "all", "--yes"])
        flash(msg, "ok" if ok else "warn")
        return redirect(url_for("runs"))

    @app.route("/actions/backup", methods=["POST"])
    def action_backup():
        try:
            meta = backup_mod.make_db_backup(reason="manual")
            if meta is None:
                flash("Nothing to back up yet — no database. Pull records first.", "warn")
            else:
                flash(f"Backup saved ({meta['db_bytes'] // 1024} KB). "
                      "Your data is now safely snapshotted.", "ok")
        except Exception as e:  # noqa: BLE001
            log.exception("manual backup failed")
            flash(f"Backup failed: {e}", "err")
        return redirect(url_for("runs"))

    @app.route("/actions/check-update", methods=["POST"])
    def action_check_update():
        info = updater.check(force=True)
        if not info.get("enabled"):
            flash("Update checking is turned off in config.json.", "warn")
        elif info.get("latest") is None:
            flash("Couldn't reach GitHub to check for updates (you may be offline, "
                  "or the repository is private).", "warn")
        elif info.get("available"):
            flash(f"Update available: v{info['current']} → v{info['latest']}.", "ok")
        else:
            flash(f"You're up to date (v{info['current']}).", "ok")
        return redirect(url_for("runs"))

    @app.route("/actions/update", methods=["POST"])
    def action_update():
        if _action_running("scrape") or _action_running("analysis"):
            flash("Wait for the running scrape/analysis to finish before updating.", "warn")
            return redirect(url_for("runs"))
        paths = project_paths()
        paths["logs"].mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = paths["logs"] / f"update-{ts}.log"
        result_path = paths["data"] / ".update_result.json"
        try:
            result_path.unlink()  # clear any prior result so the page reflects this run
        except OSError:
            pass
        # Snapshots your data, downloads the latest from GitHub, applies code only.
        ok, msg = _start_action("update", ["selfupdate.py", "apply"], log_path=str(log_path))
        if ok:
            proc = _ACTIONS.get("update")

            def _watch(p, lp, rp):
                rc = p.wait()
                try:
                    rp.write_text(json.dumps({
                        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "returncode": rc, "log": lp.name}), encoding="utf-8")
                except OSError:
                    pass
            threading.Thread(target=_watch, args=(proc, log_path, result_path),
                             daemon=True).start()
            msg = ("Updating in the background — your data is backed up first. When it "
                   "finishes, close this window, reopen with Start, then refresh.")
        flash(msg, "ok" if ok else "warn")
        return redirect(url_for("runs"))

    # ---- healthcheck ----
    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True})

    return app


def parse_args():
    p = argparse.ArgumentParser(description="Records tracker local web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--open", action="store_true",
                   help="Open the browser automatically after startup")
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    args = parse_args()
    app = create_app()
    url = f"http://{args.host}:{args.port}/"
    log.info("Starting web UI at %s", url)
    # Record the port/pid so a restore (a separate process) can detect that the
    # server is live and refuse to overwrite the database out from under it.
    try:
        paths = project_paths()
        paths["data"].mkdir(parents=True, exist_ok=True)
        lock = paths["data"] / ".server.lock"
        lock.write_text(json.dumps({"port": args.port, "pid": os.getpid()}), encoding="utf-8")
        atexit.register(lambda: lock.unlink(missing_ok=True))
    except OSError:
        pass
    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        # debug is intentionally OFF: the Werkzeug debugger is an in-browser
        # Python shell, and this port is reachable from other browser tabs.
        app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    except OSError as e:
        log.error("Could not start the web server: %s", e)
        print(
            f"\n  Could not start on port {args.port}.\n"
            "  Another program (maybe a second copy of this one) is using it.\n"
            "  Close that window, or start with a different port, e.g.:\n"
            f"      python server.py --port 8765\n",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
