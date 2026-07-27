"""Parse raw .eml bytes into a ParsedEmail (stdlib `email`)."""
from __future__ import annotations

from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.policy import default

from app.extraction.models import ParsedEmail


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def parse(raw: bytes) -> ParsedEmail:
    msg: EmailMessage = message_from_bytes(raw, policy=default)  # type: ignore[assignment]

    headers: dict[str, str] = {}
    for k, v in msg.items():
        headers.setdefault(k.lower(), str(v))

    text_body, html_body = _bodies(msg)

    return ParsedEmail(
        subject=_decode(msg["Subject"]),
        from_header=_decode(msg["From"]),
        dkim_signatures=[str(v) for v in msg.get_all("DKIM-Signature", [])],
        headers=headers,
        text_body=text_body,
        html_body=html_body,
    )


def _bodies(msg: EmailMessage) -> tuple[str, str]:
    text, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            if "attachment" in (part.get("Content-Disposition") or "").lower():
                continue
            ctype = part.get_content_type()
            try:
                content = part.get_content()
            except Exception:
                continue
            if ctype == "text/plain" and not text:
                text = content
            elif ctype == "text/html" and not html:
                html = content
    else:
        try:
            content = msg.get_content()
        except Exception:
            content = ""
        if msg.get_content_type() == "text/html":
            html = content
        else:
            text = content
    return text or "", html or ""
