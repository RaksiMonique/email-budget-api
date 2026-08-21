"""Prepare email content for extraction: HTML->text, strip footers/forward
headers, cap length."""
from __future__ import annotations

import html as _html
import re

from app.extraction.models import ParsedEmail

MAX_CHARS = 8000

_FOOTER_MARKERS = (
    "unsubscribe",
    "receiving this alert",
    "receiving this email",
    "manage alerts",
    "manage your preferences",
    "this email was sent to",
    "privacy policy",
    "all rights reserved",
)

_FWD_MARKER = re.compile(r"forwarded message|original message", re.I)
_FWD_HEADER = re.compile(
    r"^[ \t>*]*\*?(from|date|to|subject|reply-to|cc|bcc|sent)\b\s*:?", re.I
)

try:
    import html2text as _html2text  # type: ignore

    def _html_to_text(html: str) -> str:
        h = _html2text.HTML2Text()
        h.ignore_links = True
        h.ignore_images = True
        h.body_width = 0
        return h.handle(html)

except Exception:  # pragma: no cover - fallback when html2text is not installed

    def _html_to_text(html: str) -> str:
        # bounded tag matches — no unbounded ".*?" that could catastrophically
        # backtrack on pathological input (many unclosed <script>)
        text = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>", " ", html)
        text = re.sub(r"(?s)<[^>]{0,4000}>", " ", text)
        return _html.unescape(text)


def prepare(parsed: ParsedEmail) -> str:
    text = parsed.text_body.strip()
    # a stub text part ("FYI see below.") must not hide the real content in HTML
    if len(text) < 200 and parsed.html_body.strip():
        html_text = _html_to_text(parsed.html_body).strip()
        if len(html_text) > len(text):
            text = html_text

    # html2text renders HTML transaction tables as "Label | Value" rows; normalize
    # them to label-above-value so the SAME label patterns parse auto-forwarded
    # (HTML-only) alerts as well as manually-forwarded (plain-text) ones. Real
    # incident 2026-08-20: an NCB auto-forward's merchant/card came out null.
    text = _normalize_pipe_tables(text)

    # strip forwarded-quote markers, including NESTED "> > " levels
    text = re.sub(r"(?m)^[ \t]*(?:>[ \t]?)+", "", text)
    text = _strip_forward_headers(text)
    text = _strip_footer(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:MAX_CHARS]


def _strip_forward_headers(text: str) -> str:
    """Remove the RFC-header block right after a 'forwarded message' marker
    (From/Date/To/Subject/…) so a forward's own `Date:` header can't beat the
    transaction date, etc. Sender resolution already ran on the raw body, so
    dropping the quoted `From:` here is safe for extraction."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        out.append(lines[i])
        if _FWD_MARKER.search(lines[i]):
            i += 1
            # skip the contiguous header/blank lines of the forward block
            while i < len(lines) and (not lines[i].strip() or _FWD_HEADER.match(lines[i])):
                i += 1
            continue
        i += 1
    return "\n".join(out)


_TABLE_SEP = re.compile(r"^[\s|:\-]+$")  # markdown table separator / empty-pipe row


def _normalize_pipe_tables(text: str) -> str:
    """`Label | Value` (html2text's rendering of a 2-column HTML table) -> two
    lines, `Label` then `Value`. Rows that are pure separators (`---|---`, `| |`)
    are dropped; non-table lines and multi-cell rows pass through untouched."""
    out: list[str] = []
    for line in text.splitlines():
        if "|" in line:
            if _TABLE_SEP.match(line):
                continue
            cells = [c.strip() for c in line.split("|") if c.strip()]
            if len(cells) == 2:  # Label | Value
                out.append(cells[0])
                out.append(cells[1])
                continue
        out.append(line)
    return "\n".join(out)


def _strip_footer(text: str) -> str:
    kept = [
        line
        for line in text.splitlines()
        if not any(mark in line.lower() for mark in _FOOTER_MARKERS)
    ]
    return "\n".join(kept)
