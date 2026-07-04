"""Pure-logic unit tests for the highest-risk helpers (no network, no API)."""
from __future__ import annotations

from records_tracker import ai, updater
from records_tracker.database import is_support_sender, request_label
from records_tracker.mdlite import looks_like_json, markdown_to_html


# ---- markdown renderer (security-critical: must never emit raw HTML) ----
def test_markdown_escapes_html():
    out = markdown_to_html("<script>alert(1)</script> **bold**")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<strong>bold</strong>" in out


def test_markdown_lists_headings_code():
    out = markdown_to_html("# Title\n\n- one\n- two\n\n`code` and **b**")
    assert "<h3>Title</h3>" in out
    assert "<ul>" in out and "<li>one</li>" in out
    assert "<code>code</code>" in out and "<strong>b</strong>" in out


def test_markdown_only_safe_links():
    out = markdown_to_html("[ok](https://x.com) [bad](javascript:alert(1))")
    assert 'href="https://x.com"' in out
    assert 'href="javascript:' not in out  # dangerous scheme never linkified


def test_looks_like_json():
    assert looks_like_json('{"a": 1}') and looks_like_json("[1,2]")
    assert not looks_like_json("hello") and not looks_like_json("")


# ---- version comparison (drives the auto-updater) ----
def test_version_compare():
    assert updater.is_newer("1.5.0", "1.4.0")
    assert updater.is_newer("v1.10.0", "v1.9.0")      # numeric, not lexicographic
    assert not updater.is_newer("1.4.0", "1.4.0")
    assert not updater.is_newer("1.4.0", "1.5.0")
    assert updater.parse_version("v1.2.3") == (1, 2, 3)


# ---- AI helpers (no API calls) ----
def test_merge_consecutive_roles_self_heals():
    turns = [{"role": "user", "content": "a"}, {"role": "user", "content": "b"},
             {"role": "assistant", "content": "c"}, {"role": "user", "content": "d"}]
    merged = ai._merge_consecutive_roles(turns)
    assert [t["role"] for t in merged] == ["user", "assistant", "user"]
    assert merged[0]["content"] == "a\n\nb"


def test_parse_json_lenient():
    assert ai._parse_json_lenient('```json\n{"issues": []}\n```') == {"issues": []}
    assert ai._parse_json_lenient("not json at all") is None


def test_system_blocks_cache_the_reference():
    blocks = ai._system_blocks("TASK", context_block="CTX")
    assert blocks[0]["text"].startswith("You assist") and "TASK" in blocks[0]["text"]
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}        # legal reference cached
    assert blocks[2]["text"] == "CTX" and blocks[2]["cache_control"]  # chat context cached


# ---- database helpers ----
def test_is_support_sender():
    assert is_support_sender("STPETEFL Support")
    assert is_support_sender("stpetefl support team")
    assert not is_support_sender("Brad McCoy")
    assert not is_support_sender("Tech Support Inc")  # must not misbucket the requester


def test_request_label():
    assert request_label({"request_id": "P1", "short_title": "Nickname"}) == "P1 · Nickname"
    assert "Police" in request_label({"request_id": "P1", "department": "Police", "description": "x"})
    assert request_label({"request_id": "P1"}) == "P1"
    assert request_label(None) == ""
    long = request_label({"request_id": "P1", "description": "word " * 40})
    assert long.endswith("…") and len(long) < 80
