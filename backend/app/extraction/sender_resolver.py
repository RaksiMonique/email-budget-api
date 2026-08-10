"""Resolve the ORIGINAL sender of an (auto- or manually) forwarded email.

Priority: DKIM d= domain -> From: header domain -> body-embedded sender.

Forward-unwrapping: when the outer sender resolves to a *consumer mail provider*
(gmail, outlook, icloud, …) that is a forwarding fingerprint — the real sender
is the original address in the quoted "Begin forwarded message" block. A manual
forward destroys the bank's DKIM (it becomes the forwarder's gmail DKIM), so the
body is the only place the true sender survives. See docs/ingestion/forwarded-email.md.
"""
from __future__ import annotations

import re
from email.utils import parseaddr

from app.extraction.models import ParsedEmail, ResolvedSender, SenderSource

_DKIM_D = re.compile(r"\bd=([A-Za-z0-9.\-]+)")
# original "From:" inside a quoted forward — tolerate leading ">" quote markers,
# an optional display name, and addresses with or without angle brackets
_BODY_FROM = re.compile(
    r"^[>\s]*From:\s*.*?([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+)", re.MULTILINE
)
_ANY_ADDR = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")

# Personal/consumer mail providers. An email whose OUTER sender is one of these
# is almost always a forward — the real financial sender is inside the body.
CONSUMER_SENDER_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
        "msn.com", "yahoo.com", "ymail.com", "icloud.com", "me.com", "mac.com",
        "aol.com", "proton.me", "protonmail.com", "gmx.com", "zoho.com",
    }
)


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


def _body_original_sender(parsed: ParsedEmail) -> str | None:
    """The original sender from a quoted 'Begin forwarded message' block."""
    body = parsed.text_body or parsed.html_body
    m = _BODY_FROM.search(body)
    if m:
        return registered_domain(m.group(1).rsplit("@", 1)[-1])
    return None


def resolve(parsed: ParsedEmail) -> ResolvedSender:
    dkim_dom = None
    for sig in parsed.dkim_signatures:
        m = _DKIM_D.search(sig)
        if m and (dkim_dom := registered_domain(m.group(1))):
            break
    from_dom = registered_domain(_domain_of(parsed.from_header))
    outer = dkim_dom or from_dom

    # Forward-unwrapping: a consumer-provider outer sender is a forwarding
    # fingerprint — trust the original sender in the quoted body instead, but
    # only if it's a *different* (non-consumer) domain (a real bank/merchant).
    if outer in CONSUMER_SENDER_DOMAINS:
        body_dom = _body_original_sender(parsed)
        if body_dom and body_dom not in CONSUMER_SENDER_DOMAINS:
            return ResolvedSender(body_dom, SenderSource.BODY, 0.8)

    # Otherwise: DKIM (survives auto-forward) -> From: -> body fallback.
    if dkim_dom:
        return ResolvedSender(dkim_dom, SenderSource.DKIM, 0.97)
    if from_dom:
        return ResolvedSender(from_dom, SenderSource.HEADER, 0.85)

    body = parsed.text_body or parsed.html_body
    m = _BODY_FROM.search(body) or _ANY_ADDR.search(body)
    if m:
        raw = m.group(1)
        host = raw.rsplit("@", 1)[1] if "@" in raw else raw
        if dom := registered_domain(host):
            return ResolvedSender(dom, SenderSource.BODY, 0.6)

    return ResolvedSender(None, SenderSource.NONE, 0.0)
