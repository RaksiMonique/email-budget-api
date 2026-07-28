"""Detect provider forwarding-verification emails and extract the code/URL.

Gmail's "add a forwarding address" flow sends a confirmation email TO the alias,
which lands in R2 — no human inbox ever sees it. The pipeline recognizes it and
the service surfaces code + confirmation URL to the budgeting app via a
`forwarding.verification` webhook so the user can finish setup in-app.

IMPORTANT: this check runs BEFORE financial classification — the sender resolves
to google.com, which the financial registry would otherwise classify as a
Play-store receipt. Detection keys on the exact verification sender addresses
(this mail comes directly from the provider, unforwarded, so From: is reliable).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Optional

from app.extraction.models import ParsedEmail

# exact verification-sender addresses, per provider
VERIFICATION_SENDERS = {
    "forwarding-noreply@google.com": "gmail",
    "forwarding-noreply@googlemail.com": "gmail",
}

# Real-format note (observed 2026-07-28, live corpus sample): current Gmail
# verification emails carry ONLY the confirmation URL — no numeric code in
# subject or body. Code extraction below is best-effort for older/localized
# formats; consumers must treat `code` as nullable and prefer the URL.

_GMAIL_CODE_SUBJECT = re.compile(r"\(#(\d{6,12})\)")
_GMAIL_CODE_BODY = re.compile(r"(?:code|konfirmationscode)\D{0,40}?(\d{6,12})", re.I)
_GMAIL_CONFIRM_URL = re.compile(
    r"https://mail-settings\.google\.com/mail/[A-Za-z0-9_\-./?=&%]+"
)


@dataclass
class ForwardingVerification:
    provider: str
    code: Optional[str]
    confirmation_url: Optional[str]


def detect(parsed: ParsedEmail) -> Optional[ForwardingVerification]:
    _, from_addr = parseaddr(parsed.from_header or "")
    provider = VERIFICATION_SENDERS.get(from_addr.lower())
    if provider is None:
        return None

    body = parsed.text_body or parsed.html_body
    code_match = _GMAIL_CODE_SUBJECT.search(parsed.subject or "") or _GMAIL_CODE_BODY.search(body)
    url_match = _GMAIL_CONFIRM_URL.search(body)

    return ForwardingVerification(
        provider=provider,
        code=code_match.group(1) if code_match else None,
        confirmation_url=url_match.group(0) if url_match else None,
    )
