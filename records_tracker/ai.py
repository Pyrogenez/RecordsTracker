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

from .chapter119 import CHAPTER_119_REFERENCE, short_system_prefix
from .config import PROJECT_ROOT
from .database import Database, request_label

log = logging.getLogger(__name__)

# Model selection — defaults chosen for low cost without sacrificing quality:
# cheap Haiku for high-volume classification; Sonnet where reasoning quality
# matters (audits, chat, summaries). Any of these can be overridden in
# config.json under a "models" section, e.g.
#   "models": {"classify": "...", "summarize": "...", "audit": "...", "chat": "..."}
MODEL_CLASSIFY = "claude-haiku-4-5-20251001"
MODEL_CHAT = "claude-sonnet-4-6"
MODEL_AUDIT = "claude-sonnet-4-6"
MODEL_SUMMARIZE = "claude-sonnet-4-6"


def model_for(key: str, default: str) -> str:
    """Resolve a model id from config.json's optional 'models' section, falling
    back to the default. Lets the user tune the cost/quality trade-off without
    code changes."""
    try:
        raw = json.loads((PROJECT_ROOT / "config.json").read_text())
        m = (raw.get("models") or {}).get(key)
        if m and isinstance(m, str) and m.strip():
            return m.strip()
    except Exception:
        pass
    return default

# Retries for transient API errors (529/rate-limit/network) on top of the SDK's
# own handling, so a brief blip doesn't surface as a raw error to the user.
API_MAX_RETRIES = 4

# Truncation limits for packed context
ATTACHMENT_SNIPPET_LIMIT = 8000
ATTACHMENT_PACKED_LIMIT = 2500


class AIConfigError(RuntimeError):
    """Raised when the AI layer isn't properly configured (missing key, etc.)."""


class AIResponseError(RuntimeError):
    """Raised when the model returned a response we couldn't use (e.g. the audit
    JSON was malformed/truncated). Distinct from 'the record is compliant' so the
    UI never reports a broken response as a clean bill of health."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def get_api_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    # Resolve relative to the project root, NOT the current working directory,
    # so the key is found no matter where the program was launched from.
    cfg_path = PROJECT_ROOT / "config.json"
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
    return anthropic.Anthropic(api_key=key, max_retries=API_MAX_RETRIES)


def extract_text_response(resp) -> str:
    parts = []
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts)


def _log_usage(resp, label: str) -> None:
    """Best-effort token-usage logging so the user can see what AI calls cost."""
    u = getattr(resp, "usage", None)
    if u is None:
        return
    log.info(
        "AI usage [%s]: input=%s output=%s cache_read=%s cache_write=%s",
        label,
        getattr(u, "input_tokens", "?"),
        getattr(u, "output_tokens", "?"),
        getattr(u, "cache_read_input_tokens", 0),
        getattr(u, "cache_creation_input_tokens", 0),
    )


def _system_blocks(task_description: str = "",
                   context_block: str | None = None) -> list[dict]:
    """System prompt as content blocks with the large Chapter 119 reference
    marked cache_control=ephemeral. The reference is identical across audits and
    chat turns, so prompt caching makes repeat calls cheaper and faster. The
    small role+task header stays uncached because it varies per call.

    For multi-turn chat, pass context_block (the request/corpus context) so it
    too is cached — every turn after the first then reuses it instead of
    re-sending the whole record/corpus, a real token saving on long chats."""
    header = short_system_prefix()
    if task_description:
        header = header + "\n\n" + task_description.strip()
    blocks = [
        {"type": "text", "text": header},
        {"type": "text", "text": CHAPTER_119_REFERENCE,
         "cache_control": {"type": "ephemeral"}},
    ]
    if context_block:
        blocks.append({"type": "text", "text": context_block,
                       "cache_control": {"type": "ephemeral"}})
    return blocks


def _merge_consecutive_roles(turns: list[dict]) -> list[dict]:
    """Collapse adjacent same-role turns into one. The Anthropic Messages API
    requires strictly alternating user/assistant roles; merging defensively
    self-heals any conversation that was previously left with two consecutive
    user turns (see continue_conversation)."""
    merged: list[dict] = []
    for t in turns:
        if merged and merged[-1]["role"] == t["role"]:
            merged[-1]["content"] = merged[-1]["content"] + "\n\n" + t["content"]
        else:
            merged.append({"role": t["role"], "content": t["content"]})
    return merged


# ---------------------------------------------------------------------------
# Context packing
# ---------------------------------------------------------------------------

def build_request_context(db: Database, request_id: str,
                          include_attachments: bool = True,
                          max_chars: int = 60000) -> str:
    """Pack a single request + its messages + attachment text into a string
    suitable for the 'user' side of a prompt."""
    r = db.get_request(request_id)
    if not r:
        return f"(No record found for request {request_id})"
    messages = db.get_messages_for_request(request_id)
    attachments = db.get_attachments_for_request(request_id)

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
            f"### {request_label(r)}",
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
    "You're helping the user analyze their public records requests. Respond clearly "
    "and concisely. When you refer to a request, cite its ID followed by a "
    "short human label so it's recognizable, e.g. "
    "'P121302-042026 (Police — body-cam footage)'. When making legal claims, "
    "cite the specific statute section or case name from the reference. If you "
    "don't have enough information in the context to answer, say so — don't "
    "invent facts."
)


def continue_conversation(db: Database, conversation_id: int,
                          user_message: str,
                          model: str | None = None) -> str:
    """Append a user turn, call Claude with the full thread plus the request
    context (request-scoped) or corpus digest (global), save the assistant turn,
    and return the assistant text."""
    model = model or model_for("chat", MODEL_CHAT)
    client = get_client()
    conv = db.get_conversation(conversation_id)
    if not conv:
        raise ValueError(f"conversation {conversation_id} not found")

    # NOTE: we do NOT persist the user turn yet. Saving it before the API call
    # means a transient API failure leaves a dangling user message with no
    # assistant reply; the next send then produces two consecutive user turns,
    # which the Messages API rejects (roles must alternate) — wedging the thread
    # permanently. Instead we save BOTH turns together only after success.
    history = db.get_conversation_messages(conversation_id)

    # The record/corpus context is stable across a conversation's turns, so we
    # send it ONCE as a cache_control system block instead of stuffing it into a
    # user message on every turn. After the first turn it's a cache hit — a real
    # token saving on multi-turn chats.
    if conv["scope"] == "request" and conv.get("request_id"):
        context_header = (f"CONTEXT FOR REQUEST {conv['request_id']}:\n\n"
                          + build_request_context(db, conv["request_id"]))
    else:
        context_header = ("CONTEXT — DIGEST OF ALL TRACKED PUBLIC RECORDS "
                          "REQUESTS:\n\n" + build_corpus_digest(db))

    # Existing history (excluding system rows) + the new user message; collapse
    # consecutive same-role turns so an already-wedged thread self-heals.
    turns = [{"role": m["role"], "content": m["content"]}
             for m in history if m["role"] != "system"]
    turns.append({"role": "user", "content": user_message})
    api_messages = _merge_consecutive_roles(turns)

    resp = client.messages.create(
        model=model,
        max_tokens=2500,
        system=_system_blocks(_CHAT_TASK, context_block=context_header),
        messages=api_messages,
    )
    _log_usage(resp, "chat")
    reply = extract_text_response(resp).strip() or "(no response)"

    # Persist both turns atomically, only now that the call succeeded.
    with db.transaction():
        db.add_conversation_message(conversation_id, "user", user_message, commit=False)
        db.add_conversation_message(conversation_id, "assistant", reply,
                                    model=model, commit=False)
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
                             model: str | None = None) -> dict:
    """Run a Chapter 119 audit on a single request. Persists findings to the
    compliance_issues table and returns the structured result."""
    model = model or model_for("audit", MODEL_AUDIT)
    client = get_client()
    context = build_request_context(db, request_id)
    if not context:
        return {"issues": [], "overall_assessment": "No record found."}

    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        system=_system_blocks(_AUDIT_TASK),
        messages=[{"role": "user", "content": context}],
    )
    _log_usage(resp, "audit")
    raw = extract_text_response(resp)
    parsed = _parse_json_lenient(raw)
    if parsed is None:
        # Do NOT fall back to an empty issues list — that would render an
        # unreadable/truncated response as a reassuring "0 issues / compliant".
        log.warning(
            "Audit for %s returned unparseable JSON (%d chars). Head: %s",
            request_id, len(raw or ""), (raw or "")[:500],
        )
        raise AIResponseError(
            "The audit ran but the AI's response could not be read. "
            "This is usually transient — please try again."
        )

    issues = parsed.get("issues") or []
    saved_ids: list[int] = []
    conv_title = f"Compliance audit — {request_id}"
    existing = [
        c for c in db.list_conversations(scope="request", request_id=request_id)
        if c["title"] == conv_title
    ]
    # Persist everything in ONE transaction: replace prior AI findings for this
    # request (so re-running doesn't duplicate), insert the fresh batch, and
    # record the transcript. User-logged and already-triaged issues are kept.
    with db.transaction():
        db.clear_open_ai_issues(request_id, commit=False)
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
                }, commit=False)
                saved_ids.append(iid)
            except Exception:
                log.exception("failed to save compliance issue for %s", request_id)
        if existing:
            conv_id = existing[0]["conversation_id"]
        else:
            conv_id = db.create_conversation(
                "request", conv_title, request_id=request_id, commit=False)
        db.add_conversation_message(
            conv_id, "user",
            "Please audit this request for Chapter 119 compliance.", commit=False)
        db.add_conversation_message(
            conv_id, "assistant",
            json.dumps(parsed, indent=2, default=str), model=model, commit=False)

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
                      model: str | None = None) -> str:
    model = model or model_for("summarize", MODEL_SUMMARIZE)
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
