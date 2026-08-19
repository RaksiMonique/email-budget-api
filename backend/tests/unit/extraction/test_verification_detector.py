"""Regression: Gmail forwarding-confirmation URL extraction.

Real incident (2026-08-18): Gmail's *resend* of a forwarding confirmation uses a
different host (mail.google.com) than the original (mail-settings.google.com), so
the URL came through null and the receiver dropped it. Both hosts must extract,
and the /mail/uf- CANCEL link (present in the same email) must never be surfaced.
"""
from app.extraction import verification_detector as V
from app.extraction.models import ParsedEmail

CONFIRM_SETTINGS = "https://mail-settings.google.com/mail/vf-%5BABC123%5D-tail"
CONFIRM_MAIL = "https://mail.google.com/mail/vf-%5BXYZ789%5D-tail"
CANCEL_MAIL = "https://mail.google.com/mail/uf-%5BDEF456%5D-tail"


def _email(body: str) -> ParsedEmail:
    return ParsedEmail(
        subject="(Gmail Forwarding Confirmation - Receive Mail from x@gmail.com)",
        from_header="Gmail Team <forwarding-noreply@google.com>",
        dkim_signatures=[],
        headers={},
        text_body=body,
        html_body="",
    )


def test_original_host_mail_settings():
    v = V.detect(_email(f"please click:\n{CONFIRM_SETTINGS}\nThanks, Gmail"))
    assert v is not None and v.confirmation_url == CONFIRM_SETTINGS


def test_resend_host_mail_google():
    # the "Re-send" variant — must still extract (this is the incident case)
    v = V.detect(_email(f"please click:\n{CONFIRM_MAIL}\nThanks, Gmail"))
    assert v is not None and v.confirmation_url == CONFIRM_MAIL


def test_picks_confirm_never_cancel():
    # the real email carries BOTH a confirm (vf-) and a cancel (uf-) link
    body = f"To confirm:\n{CONFIRM_MAIL}\nTo cancel this verification:\n{CANCEL_MAIL}\n"
    v = V.detect(_email(body))
    assert v is not None and v.confirmation_url == CONFIRM_MAIL


def test_non_verification_sender_ignored():
    e = ParsedEmail(
        subject="hi", from_header="someone@example.com", dkim_signatures=[],
        headers={}, text_body=CONFIRM_MAIL, html_body="",
    )
    assert V.detect(e) is None
