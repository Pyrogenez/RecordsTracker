"""Local web UI for the Public Records tracker.

Launches a Flask server on 127.0.0.1 so the user can browse requests, chat with
Claude about a specific record, run a Chapter 119 compliance audit, and
review flagged issues across every request.

Usage:
    python server.py               # serves on http://127.0.0.1:5000
    python server.py --port 8765
    python server.py --open        # opens browser automatically
"""
from __future__ import annotations

import argparse
import logging
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template, request,
    url_for,
)

from records_tracker import ai as ai_mod
from records_tracker.config import project_paths
from records_tracker.database import Database

log = logging.getLogger("server")


def create_app() -> Flask:
    paths = project_paths()
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.secret_key = "stpete-records-tracker-local-ui"
    app.config["DB_PATH"] = paths["database"]
    app.config["DOWNLOADS_DIR"] = paths["downloads"]
    app.config["EXCEL_PATH"] = paths["excel"]

    # ----- DB per-request -----
    def get_db() -> Database:
        if "db" not in g:
            g.db = Database(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def close_db(exception=None):  # noqa: ARG001
        db = g.pop("db", None)
        if db is not None:
            db.close()

    # ----- filters -----
    @app.template_filter("fmt_dt")
    def fmt_dt(value):
        if not value:
            return ""
        try:
            # Handle both 'YYYY-MM-DDTHH:MM:SS+00:00' and 'M/D/YYYY H:MM:SS AM/PM'
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

    # ----- routes -----

    @app.route("/")
    def index():
        db = get_db()
        requests_ = db.get_all_requests()
        counts = db.counts()
        last = db.get_last_run()
        open_issues_by_req: dict[str, int] = {}
        for iss in db.get_compliance_issues(status="open"):
            open_issues_by_req[iss["request_id"]] = open_issues_by_req.get(
                iss["request_id"], 0,
            ) + 1
        # Open-ness helper (uses portal final + override)
        for r in requests_:
            r["_is_closed"] = db.is_request_closed(r["request_id"], r.get("final_state"))
            r["_open_issues"] = open_issues_by_req.get(r["request_id"], 0)
        return render_template(
            "index.html",
            requests=requests_,
            counts=counts,
            last_run=last,
        )

    @app.route("/requests/<request_id>")
    def request_detail(request_id: str):
        db = get_db()
        reqs = [r for r in db.get_all_requests() if r["request_id"] == request_id]
        if not reqs:
            abort(404)
        r = reqs[0]
        messages = db.get_messages_for_request(request_id)
        # Attach AI classifications to messages for the template
        for m in messages:
            cls = db.get_message_classification(m["message_id"])
            m["_classification"] = cls["classification"] if cls else None
        attachments = [a for a in db.get_all_attachments()
                       if a["request_id"] == request_id]
        override = db.get_override(request_id)
        summary = db.get_request_summary(request_id)
        issues = db.get_compliance_issues(request_id=request_id)
        conversations = db.list_conversations(scope="request", request_id=request_id)
        is_closed = db.is_request_closed(request_id, r.get("final_state"))
        return render_template(
            "request_detail.html",
            r=r,
            is_closed=is_closed,
            messages=messages,
            attachments=attachments,
            override=override,
            summary=summary,
            issues=issues,
            conversations=conversations,
        )

    @app.route("/requests/<request_id>/audit", methods=["POST"])
    def request_audit(request_id: str):
        db = get_db()
        try:
            result = ai_mod.audit_request_compliance(db, request_id)
            n = len(result.get("issues") or [])
            flash(f"Audit complete — {n} issue(s) logged.", "ok")
        except ai_mod.AIConfigError as e:
            flash(str(e), "err")
        except Exception as e:  # noqa: BLE001
            log.exception("audit failed")
            flash(f"Audit failed: {e}", "err")
        return redirect(url_for("request_detail", request_id=request_id))

    @app.route("/requests/<request_id>/summarize", methods=["POST"])
    def request_summarize(request_id: str):
        db = get_db()
        try:
            ai_mod.summarize_request(db, request_id)
            flash("Summary refreshed.", "ok")
        except ai_mod.AIConfigError as e:
            flash(str(e), "err")
        except Exception as e:  # noqa: BLE001
            log.exception("summarize failed")
            flash(f"Summarize failed: {e}", "err")
        return redirect(url_for("request_detail", request_id=request_id))

    @app.route("/requests/<request_id>/override", methods=["POST"])
    def request_override(request_id: str):
        db = get_db()
        is_closed = bool(request.form.get("is_closed"))
        notes = (request.form.get("notes") or "").strip() or None
        msg_id_raw = (request.form.get("first_real_reply_message_id") or "").strip()
        msg_id: int | None = None
        if msg_id_raw:
            try:
                msg_id = int(msg_id_raw)
            except ValueError:
                flash("first_real_reply_message_id must be an integer.", "err")
                return redirect(url_for("request_detail", request_id=request_id))
        db.upsert_override(request_id, msg_id, notes, is_closed=is_closed)
        with db.transaction():
            db.recompute_first_reply(request_id)
        flash("Override saved.", "ok")
        return redirect(url_for("request_detail", request_id=request_id))

    # ---- conversations (both scopes go through here) ----

    @app.route("/conversations/new", methods=["POST"])
    def conversation_new():
        db = get_db()
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
        return render_template(
            "conversation.html",
            conv=conv,
            messages=messages,
        )

    @app.route("/conversations/<int:conversation_id>/send", methods=["POST"])
    def conversation_send(conversation_id: int):
        db = get_db()
        user_text = (request.form.get("message") or "").strip()
        if not user_text:
            flash("Message cannot be empty.", "err")
            return redirect(url_for("conversation_view",
                                    conversation_id=conversation_id))
        try:
            ai_mod.continue_conversation(db, conversation_id, user_text)
        except ai_mod.AIConfigError as e:
            flash(str(e), "err")
        except Exception as e:  # noqa: BLE001
            log.exception("chat failed")
            flash(f"Chat failed: {e}", "err")
        return redirect(url_for("conversation_view",
                                conversation_id=conversation_id))

    @app.route("/conversations/<int:conversation_id>/delete", methods=["POST"])
    def conversation_delete(conversation_id: int):
        db = get_db()
        conv = db.get_conversation(conversation_id)
        if not conv:
            abort(404)
        db.delete_conversation(conversation_id)
        flash("Conversation deleted.", "ok")
        if conv["scope"] == "request" and conv.get("request_id"):
            return redirect(url_for("request_detail",
                                    request_id=conv["request_id"]))
        return redirect(url_for("global_analysis"))

    @app.route("/analysis")
    def global_analysis():
        db = get_db()
        conversations = db.list_conversations(scope="global")
        return render_template("global_analysis.html", conversations=conversations)

    @app.route("/compliance")
    def compliance_dashboard():
        db = get_db()
        status_filter = request.args.get("status") or "open"
        if status_filter == "all":
            issues = db.get_compliance_issues()
        else:
            issues = db.get_compliance_issues(status=status_filter)
        # Group by request for easier scanning
        by_request: dict[str, list[dict]] = {}
        for iss in issues:
            by_request.setdefault(iss["request_id"], []).append(iss)
        return render_template(
            "compliance.html",
            issues=issues,
            by_request=by_request,
            status_filter=status_filter,
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
        data = {
            "request_id": request.form.get("request_id"),
            "statute_section": request.form.get("statute_section") or None,
            "issue_type": request.form.get("issue_type") or "manual",
            "severity": request.form.get("severity") or "medium",
            "description": request.form.get("description") or "",
            "evidence": request.form.get("evidence") or None,
            "identified_by": "user",
            "status": "open",
        }
        if not data["request_id"] or not data["description"]:
            flash("request_id and description are required.", "err")
            return redirect(request.referrer or url_for("compliance_dashboard"))
        db.add_compliance_issue(data)
        flash("Issue logged.", "ok")
        return redirect(request.referrer or url_for("compliance_dashboard"))

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
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    args = parse_args()
    app = create_app()
    url = f"http://{args.host}:{args.port}/"
    log.info("Starting web UI at %s", url)
    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
