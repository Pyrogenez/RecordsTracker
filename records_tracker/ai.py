"""Shared AI helpers used by the web UI and CLI.

Centralizes:
  * Anthropic client construction
  * Context packing for a single request (messages + attachment text)
  * Saved-conversation chat turn
  * Chapter 119 compliance audit (saves structured issues to the DB)
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from .chapter119 import full_system_prompt, short_system_prefix
from .database import Database

log = logging.getLogger(__name__)

# Model selection
MODEL_CLASSIFY = "claude-haiku-4-5-20251001"
MODEL_CHAT = "claude-sonnet-4-6"
MODEL_AUDIT = "claude-sonnet-4-6"
MODEL_SUMMARIZE = "claude-sonnet-4-6"

# Truncation limits for packed context
ATTACHMENT_SNIPPET_LIMIT = 8000
ATTACHMENT_PACKED_LIMIT = 2500


class AIConfigError(RuntimeError):
    """Raised when the AI layer isn't properly configured (missing key, etc.)."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def get_api_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    cfg_path = Path("config.json")
    if cfg_path.exists():
        try:
            raw = json.loads(cfg_path.read_text())
            k = raw.get("anthropic_api_key")
            if k and isinstance(k, str) and k.strip():
                return k.strip()
        except Exception:
            pass
    return None


def get_client():
    """Return an Anthropic client. Raises AIConfigError if misconfigured."""
    key = get_api_key()
    if not key:
        raise AIConfigError(
            "No Anthropic API key found. Set ANTHROPIC_API_KEY env var, or add "
            '"anthropic_api_key" to config.json.'
        )
    try:
        import anthropic
    except ImportError as e:
        raise AIConfigError(
            "The 'anthropic' package is not installed. Run: "
            "python -m pip install -r requirements.txt"
        ) from e
    return anthropic.Anthropic(api_key=key)


def extract_text_response(resp) -> str:
    parts = []
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Context packing
# ---------------------------------------------------------------------------

def build_request_context(db: Database, request_id: str,
                          include_attachments: bool = True,
                          max_chars: int = 60000) -> str:
    """Pack a single request + its messages + attachment text into a string
    suitable for the 'user' side of a prompt."""
    reqs = [r for r in db.get_all_requests() if r["request_id"] == request_id]
    if not reqs:
        return f"(No record found for request {request_id})"
    r = reqs[0]
    messages = db.get_messages_for_request(request_id)
    attachments = [a for a in db.get_all_attachments()
                   if a["request_id"] == request_id]

    lines = []
    lines.append(f"REQUEST: {r['request_id']}")
    lines.append(f"  status:            {r.get('status')}")
    lines.append(f"  portal final:      {r.get('final_state')}")
    lines.append(f"  type:              {r.get('request_type')}")
    lines.append(f"  category:          {r.get('category')}")
    lines.append(f"  department:        {r.get('department')}")
    lines.append(f"  records type:      {r.get('records_type')}")
    lines.append(f"  submitted:         {r.get('submission_time')}")
    lines.append(f"  first auto-ack:    {r.get('first_auto_ack_time')}")
    lines.append(f"  first real reply:  {r.get('first_real_reply_time')}")
    lines.append(f"  hours to reply:    {r.get('hours_to_first_reply')}")
    lines.append(f"  last modified:     {r.get('last_modified_at')}")
    lines.append("")
    lines.append(f"DESCRIPTION:\n{r.get('description') or '(none)'}")
    lines.append("")
    lines.append("MESSAGES (chronological):")
    for m in messages:
        body = (m.get("body") or "").strip()
        lines.append(f"--- msg {m['message_id']} | {m['sent_at']} | {m['sender']} ---")
        if m.get("subject"):
            lines.append(f"Subject: {m['subject']}")
        if body:
            lines.append(body[:3000])
        lines.append("")

    if include_attachments and attachments:
        lines.append("ATTACHMENTS:")
        for a in attachments:
            size_str = f"{a['file_size']} bytes" if a.get("file_size") else "?"
            lines.append(
                f"* {a['filename']} (id={a['attachment_id']}, "
                f"{a['download_status']}, {size_str})"
            )
            txt = db.get_attachment_text(a["attachment_id"])
            if txt and txt.get("extracted_text"):
                snippet = txt["extracted_text"][:ATTACHMENT_PACKED_LIMIT]
                lines.append("  --- extracted text (truncated) ---")
                lines.append(snippet)
                lines.append("  --- end ---")

    ovs = db.get_override(request_id)
    if ovs:
        lines.append("")
        lines.append(f"USER OVERRIDES: {ovs}")

    issues = db.get_compliance_issues(request_id=request_id)
    if issues:
        lines.append("")
        lines.append("EXISTING COMPLIANCE ISSUES LOGGED:")
        for i in issues:
            lines.append(
                f"  - [{i['status']}] {i['statute_section'] or ''} "
                f"{i['issue_type']}: {i['description'][:200]}"
            )

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[...context truncated...]"
    return text


def build_corpus_digest(db: Database, max_chars: int = 160_000) -> str:
    """A lightweight digest of all requests for cross-record analysis."""
    lines = []
    summaries = {s["request_id"]: s for s in db.get_all_request_summaries()}
    all_issues = db.get_compliance_issues()
    issues_by_req: dict[str, list[dict]] = {}
    for iss in all_issues:
        issues_by_req.setdefault(iss["request_id"], []).append(iss)
    for r in db.get_all_requests():
        rid = r["request_id"]
        block = [
            f"### {rid}",
            f"status={r.get('status')}/{r.get('final_state')}, "
            f"dept={r.get('department')}, type={r.get('request_type')}",
            f"submitted={r.get('submission_time')}, "
            f"first_reply={r.get('first_real_reply_time')}, "
            f"hours_to_reply={r.get('hours_to_first_reply')}",
        ]
        desc = (r.get("description") or "").strip()
        if desc:
            block.append(f"description: {desc[:400]}")
        s = summaries.get(rid)
        if s:
            block.append(f"summary: {s['summary'][:800]}")
        iss = issues_by_req.get(rid, [])
        if iss:
            block.append(
                f"compliance_issues: "
                + "; ".join(
                    f"{i['issue_type']} ({i['statute_section'] or 'n/a'}, "
                    f"{i['severity'] or 'n/a'})"
                    for i in iss
                )
            )
        lines.append("\n".join(block))
    text = "\n\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[...corpus truncated...]"
    return text


# ---------------------------------------------------------------------------
# Chat turn (saved conversations)
# ---------------------------------------------------------------------------

_CHAT_TASK = (
    "You're helping the user analyze his public records requests. Respond clearly "
    "and concisely. Cite specific requests by their ID (e.g. P121302-042026) "
    "when discussing them. When making legal claims, cite the specific statute "
    "section or case name from the reference. If you don't have enough "
    "information in the context to answer, say so — don't invent facts."
)


def continue_conversation(db: Database, conversation_id: int,
                          user_message: str,
                          model: str = MODEL_CHAT) -> str:
    """Append a user turn, call Claude with the full thread + request context
    (if this is a request-scoped conversation) or the corpus digest (if
    global), save the assistant turn, and return the assistant text."""
    client = get_client()
    conv = db.get_conversation(conversation_id)
    if not conv:
        raise ValueError(f"conversation {conversation_id} not found")

    db.add_conversation_message(conversation_id, "user", user_message)

    history = db.get_conversation_messages(conversation_id)
    # Build context block
    if conv["scope"] == "request" and conv.get("request_id"):
        context_block = build_request_context(db, conv["request_id"])
        context_header = f"CONTEXT FOR REQUEST {conv['request_id']}:\n\n{context_block}"
    else:
        context_block = build_corpus_digest(db)
        context_header = (
            "CONTEXT — DIGEST OF ALL TRACKED PUBLIC RECORDS REQUESTS:\n\n"
            + context_block
        )

    # The first user turn in the API conversation carries the context block;
    # subsequent turns carry only the user message so we don't blow the budget.
    api_messages = []
    first_user_turn = True
    for m in history:
        if m["role"] == "system":
            continue
        content = m["content"]
        if m["role"] == "user" and first_user_turn:
            content = context_header + "\n\n---\n\nUSER:\n" + content
            first_user_turn = False
        api_messages.append({"role": m["role"], "content": content})

    resp = client.messages.create(
        model=model,
        max_tokens=2500,
        system=full_system_prompt(_CHAT_TASK),
        messages=api_messages,
    )
    reply = extract_text_response(resp).strip() or "(no response)"
    db.add_conversation_message(conversation_id, "assistant", reply, model=model)
    return reply


# ---------------------------------------------------------------------------
# Compliance audit
# ---------------------------------------------------------------------------

_AUDIT_TASK = (
    "TASK: Audit this public records request for potential violations of "
    "Florida Public Records Law (Chapter 119). Be specific and conservative. "
    "Return a JSON object with an 'issues' array. Each issue object must have:\n"
    "  - statute_section   (e.g. '119.07(1)(a)' or null if general)\n"
    "  - issue_type        (short snake_case label, e.g. 'unreasonable_delay',\n"
    "                       'blanket_denial', 'excessive_fee', 'no_written_exemption',\n"
    "                       'inspection_fee', 'identification_demand', 'redaction_without_basis')\n"
    "  - severity          ('low' | 'medium' | 'high')\n"
    "  - description       (1-3 sentences — what the agency did or failed to do)\n"
    "  - evidence          (a direct quote or message_id reference from the record)\n"
    "  - confidence        (0.0-1.0 — how confident you are this is a real violation)\n"
    "Also include an 'overall_assessment' string (1-2 paragraphs) and a "
    "'recommended_actions' array of short strings.\n\n"
    "If the record appears fully compliant, return an empty issues array and "
    "say so in the overall_assessment.\n\n"
    "RETURN JSON ONLY. No markdown, no code fences, no prose before/after the JSON."
)


def _parse_json_lenient(text: str) -> dict | None:
    """Tolerant JSON parse — strip code fences, find outermost object."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    i = text.find("{")
    j = text.rfind("}")
    if i == -1 or j == -1 or j < i:
        return None
    try:
        return json.loads(text[i:j + 1])
    except Exception:
        return None


def audit_request_compliance(db: Database, request_id: str,
                             model: str = MODEL_AUDIT) -> dict:
    """Run a Chapter 119 audit on a single request. Persists findings to the
    compliance_issues table and returns the structured result."""
    client = get_client()
    context = build_request_context(db, request_id)
    if not context:
        return {"issues": [], "overall_assessment": "No record found."}

    resp = client.messages.create(
        model=model,
        max_tokens=3000,
        system=full_system_prompt(_AUDIT_TASK),
        messages=[{"role": "user", "content": context}],
    )
    raw = extract_text_response(resp)
    parsed = _parse_json_lenient(raw) or {"issues": [], "overall_assessment": raw[:2000]}

    # Persist issues (open by default). Also record the audit as a saved
    # conversation turn so the user can see what the AI said.
    issues = parsed.get("issues") or []
    saved_ids: list[int] = []
    for it in issues:
        try:
            iid = db.add_compliance_issue({
                "request_id": request_id,
                "statute_section": it.get("statute_section"),
                "issue_type": (it.get("issue_type") or "unspecified")[:80],
                "severity": (it.get("severity") or "medium"),
                "description": it.get("description") or "",
                "evidence": it.get("evidence"),
                "ai_confidence": float(it.get("confidence")) if it.get("confidence") is not None else None,
                "identified_by": "ai",
                "model": model,
                "status": "open",
            })
            saved_ids.append(iid)
        except Exception:
            log.exception("failed to save compliance issue for %s", request_id)

    # Also record the audit transcript in a conversation so it's reviewable.
    conv_title = f"Compliance audit — {request_id}"
    existing = [
        c for c in db.list_conversations(scope="request", request_id=request_id)
        if c["title"] == conv_title
    ]
    if existing:
        conv_id = existing[0]["conversation_id"]
    else:
        conv_id = db.create_conversation("request", conv_title, request_id=request_id)
    db.add_conversation_message(
        conv_id, "user",
        "Please audit this request for Chapter 119 compliance.",
    )
    db.add_conversation_message(
        conv_id, "assistant",
        json.dumps(parsed, indent=2, default=str),
        model=model,
    )

    parsed["_saved_issue_ids"] = saved_ids
    parsed["_conversation_id"] = conv_id
    return parsed


# ---------------------------------------------------------------------------
# Per-request summary (reused from analyze.py style)
# ---------------------------------------------------------------------------

_SUMMARIZE_TASK = (
    "TASK: Summarize this public records request. Cover: (1) what was "
    "requested, (2) what the city has provided, (3) current status, "
    "(4) any red flags or notable dates. Keep it concise — 1-2 short "
    "paragraphs. Don't editorialize. If the record is pending, say so."
)


def summarize_request(db: Database, request_id: str,
                      model: str = MODEL_SUMMARIZE) -> str:
    client = get_client()
    ctx = build_request_context(db, request_id)
    resp = client.messages.create(
        model=model,
        max_tokens=700,
        system=short_system_prefix() + "\n\n" + _SUMMARIZE_TASK,
        messages=[{"role": "user", "content": ctx}],
    )
    text = extract_text_response(resp).strip()
    if text:
        db.upsert_request_summary(request_id, text, model)
    return text
