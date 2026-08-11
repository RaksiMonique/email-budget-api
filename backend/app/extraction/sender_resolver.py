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
# original "From:" inside a quoted/forwarded block. Tolerate the different ways
# mail clients render it: ">" quote markers (Apple Mail), "*From:*" asterisks
# (Gmail forward reformatting), an optional display name, and addresses with or
# without angle brackets.
_BODY_FROM = re.compile(
    r"^[>\s*]*From:\**\s*.*?([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+)", re.MULTILINE
)
_ANY_ADDR = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")

# Personal/consumer mail providers. An email whose OUTER sender is one of these
# is almost always a forward — the real financial sender is inside the body. An
# intermediate forwarder on one of these must be SKIPPED to reach the bank.
CONSUMER_SENDER_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
        "msn.com", "yahoo.com", "ymail.com", "rocketmail.com", "icloud.com",
        "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com",
        "protonmail.ch", "pm.me", "gmx.com", "gmx.net", "zoho.com", "mail.com",
        "fastmail.com", "hey.com", "comcast.net", "verizon.net", "att.net",
        "sbcglobal.net", "cox.net", "btinternet.com",
        # common regional variants (registered_domain keeps 3 labels for these)
        "yahoo.co.uk", "yahoo.co.in", "yahoo.ca", "yahoo.fr", "yahoo.de",
        "hotmail.co.uk", "outlook.co.uk", "live.co.uk",
    }
)

# multi-part public suffixes where the registrable domain is the LAST THREE
# labels (bank.com.jm, alice.co.uk) — naive last-two would wrongly yield com.jm
_MULTI_TLD = frozenset(
    {
        "co.uk", "org.uk", "gov.uk", "ac.uk", "me.uk", "ltd.uk", "plc.uk",
        "co.jm", "com.jm", "org.jm", "net.jm", "edu.jm", "gov.jm",
        "co.za", "org.za", "com.au", "net.au", "org.au", "co.nz",
        "com.br", "co.in", "com.mx", "com.sg", "com.hk", "co.ke", "co.tt",
    }
)


def registered_domain(host: str | None) -> str | None:
    """Reduce a host to its registrable domain (chase.com from email.chase.com).

    Handles the multi-part suffixes that matter here — Jamaica's `.com.jm`/`.co.jm`
    and `.co.uk` etc. — so `bank.com.jm` is not mangled to `com.jm`.
    """
    if not host:
        return None
    host = host.strip().strip(".").lower()
    labels = [x for x in host.split(".") if x]
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_TLD:
        return ".".join(labels[-3:])
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return host or None


def _domain_of(addr: str) -> str | None:
    _, email_addr = parseaddr(addr or "")
    if "@" in email_addr:
        return email_addr.rsplit("@", 1)[1]
    return None


def _scan_from(source: str) -> str | None:
    for m in _BODY_FROM.finditer(source):
        dom = registered_domain(m.group(1).rsplit("@", 1)[-1])
        if dom and dom not in CONSUMER_SENDER_DOMAINS:
            return dom
    return None


def _body_original_sender(parsed: ParsedEmail) -> str | None:
    """The original sender from a quoted forward block. Scans ALL 'From:' lines
    and returns the first NON-consumer domain — so a forward-of-a-forward skips
    intermediate personal accounts (gmail/…) and reaches the real bank. Scans the
    text part first, then the HTML part (a forward's real content may be HTML-only
    even when a stub text part exists)."""
    if parsed.text_body.strip() and (dom := _scan_from(parsed.text_body)):
        return dom
    if parsed.html_body.strip():
        from app.extraction.content_preparer import _html_to_text

        return _scan_from(_html_to_text(parsed.html_body))
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

    body = _body_text(parsed)
    m = _BODY_FROM.search(body) or _ANY_ADDR.search(body)
    if m:
        raw = m.group(1)
        host = raw.rsplit("@", 1)[1] if "@" in raw else raw
        if dom := registered_domain(host):
            return ResolvedSender(dom, SenderSource.BODY, 0.6)

    return ResolvedSender(None, SenderSource.NONE, 0.0)
