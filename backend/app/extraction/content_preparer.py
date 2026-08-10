"""Prepare email content for extraction: HTML->text, strip footers, cap length."""
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
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        return _html.unescape(text)


def prepare(parsed: ParsedEmail) -> str:
    text = parsed.text_body.strip() or _html_to_text(parsed.html_body)
    # strip forwarded-quote markers, including NESTED ones ("> > ") from a
    # forward-of-a-forward, so label/value lines read like directly-received mail
    text = re.sub(r"(?m)^[ \t]*(?:>[ \t]?)+", "", text)
    text = _strip_footer(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:MAX_CHARS]


def _strip_footer(text: str) -> str:
    kept = [
        line
        for line in text.splitlines()
        if not any(mark in line.lower() for mark in _FOOTER_MARKERS)
    ]
    return "\n".join(kept)
