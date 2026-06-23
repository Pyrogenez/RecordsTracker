"""A tiny, safe Markdown-subset renderer for AI-generated text.

Why server-side and home-grown: the only Markdown we render is our own AI
output (summaries, chat replies, audit assessments). We want it formatted
without pulling in a dependency or a client-side bundle, and without ever
trusting raw HTML. So this escapes EVERYTHING first, then re-introduces a
conservative subset of formatting. There is no path by which input HTML/script
survives — `markdown_to_html` is safe to mark |safe in templates.

Supported: fenced code (```), headings (#..###), unordered/ordered lists,
**bold**, *italic* / _italic_, `inline code`, [text](http/https/relative link),
blank-line paragraphs, and hard line breaks within a paragraph.
"""
from __future__ import annotations

import html
import re

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)|_(.+?)_")
_CODE = re.compile(r"`([^`]+?)`")
_LINK = re.compile(r"\[([^\]]+?)\]\((https?://[^\s)]+|/[^\s)]*)\)")


def _inline(text: str) -> str:
    """Apply inline formatting to an already-HTML-escaped line."""
    # Links first (so their text can still get bold/italic/code afterwards).
    text = _LINK.sub(
        lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>',
        text,
    )
    text = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", text)
    text = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", text)
    text = _ITALIC.sub(
        lambda m: f"<em>{m.group(1) if m.group(1) is not None else m.group(2)}</em>",
        text,
    )
    return text


def markdown_to_html(text: str | None) -> str:
    if not text:
        return ""
    escaped = html.escape(str(text))
    out: list[str] = []
    list_type: str | None = None  # 'ul' | 'ol' | None
    para: list[str] = []
    in_code = False
    code_buf: list[str] = []

    def close_list() -> None:
        nonlocal list_type
        if list_type:
            out.append(f"</{list_type}>")
            list_type = None

    def flush_para() -> None:
        if para:
            out.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()

    for raw_line in escaped.split("\n"):
        line = raw_line.rstrip()

        # Fenced code blocks: collect verbatim until the closing fence.
        if line.strip().startswith("```"):
            if in_code:
                out.append("<pre class=\"md-code\"><code>" + "\n".join(code_buf) + "</code></pre>")
                code_buf = []
                in_code = False
            else:
                flush_para()
                close_list()
                in_code = True
            continue
        if in_code:
            code_buf.append(raw_line)
            continue

        if not line.strip():
            flush_para()
            close_list()
            continue

        heading = re.match(r"^(#{1,3})\s+(.*)$", line)
        if heading:
            flush_para()
            close_list()
            level = len(heading.group(1)) + 2  # # -> h3, ## -> h4, ### -> h5
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue

        ul = re.match(r"^\s*[-*]\s+(.*)$", line)
        ol = re.match(r"^\s*\d+\.\s+(.*)$", line)
        if ul or ol:
            flush_para()
            want = "ul" if ul else "ol"
            if list_type != want:
                close_list()
                out.append(f"<{want}>")
                list_type = want
            out.append(f"<li>{_inline((ul or ol).group(1))}</li>")
            continue

        close_list()
        para.append(_inline(line.strip()))

    if in_code:  # unterminated fence
        out.append("<pre class=\"md-code\"><code>" + "\n".join(code_buf) + "</code></pre>")
    flush_para()
    close_list()
    return "\n".join(out)


def looks_like_json(text: str | None) -> bool:
    """True if a string is (probably) a raw JSON object/array dump — used to keep
    saved audit transcripts in a <pre> block rather than mangling them."""
    if not text:
        return False
    s = str(text).strip()
    return (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]"))
