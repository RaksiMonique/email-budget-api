"""Resolve the ORIGINAL sender of an (auto-)forwarded email.

Priority: DKIM d= domain  ->  From: header domain  ->  body-embedded sender.
See docs/ingestion/forwarded-email.md.
"""
from __future__ import annotations

import re
from email.utils import parseaddr

from app.extraction.models import ParsedEmail, ResolvedSender, SenderSource

_DKIM_D = re.compile(r"\bd=([A-Za-z0-9.\-]+)")
_BODY_FROM = re.compile(
    r"^\s*From:\s*.*?<?([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+)>?", re.MULTILINE
)
_ANY_ADDR = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")


def registered_domain(host: str | None) -> str | None:
    """Naive eTLD reduction: keep the last two labels (chase.com from email.chase.com).

    Good enough for MVP's known senders; multi-part TLDs (co.uk) are a Phase 2 concern.
    """
    if not host:
        return None
    host = host.strip().strip(".").lower()
    labels = [x for x in host.split(".") if x]
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return host or None


def _domain_of(addr: str) -> str | None:
    _, email_addr = parseaddr(addr or "")
    if "@" in email_addr:
        return email_addr.rsplit("@", 1)[1]
    return None


def resolve(parsed: ParsedEmail) -> ResolvedSender:
    # 1. DKIM d= (survives forwarding; hardest to spoof)
    for sig in parsed.dkim_signatures:
        m = _DKIM_D.search(sig)
        if m and (dom := registered_domain(m.group(1))):
            return ResolvedSender(dom, SenderSource.DKIM, 0.97)

    # 2. From: header (preserved by server-side auto-forward)
    if dom := registered_domain(_domain_of(parsed.from_header)):
        return ResolvedSender(dom, SenderSource.HEADER, 0.85)

    # 3. Body-embedded sender (best-effort fallback for header-rewriting providers)
    body = parsed.text_body or parsed.html_body
    m = _BODY_FROM.search(body) or _ANY_ADDR.search(body)
    if m:
        raw = m.group(1)
        host = raw.rsplit("@", 1)[1] if "@" in raw else raw
        if dom := registered_domain(host):
            return ResolvedSender(dom, SenderSource.BODY, 0.6)

    return ResolvedSender(None, SenderSource.NONE, 0.0)
