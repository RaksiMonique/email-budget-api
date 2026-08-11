"""Regression tests for content preparation: forward-header stripping (so a
forward envelope's Date:/From: can't hijack extraction) and richer-source
selection (a stub text part must not hide the real content in the HTML part)."""
from datetime import date
from decimal import Decimal

from app.extraction import content_preparer, general_extractor as G
from app.extraction.models import ParsedEmail


def _parsed(*, text="", html=""):
    return ParsedEmail(
        subject="", from_header="", dkim_signatures=[], headers={},
        text_body=text, html_body=html,
    )


def test_forward_header_date_does_not_beat_transaction_date():
    """The forward envelope's own 'Date:' header (Mon, 09 ...) must be stripped so
    the real Transaction Date inside the alert wins."""
    body = (
        "Sent from my iPhone\n\n"
        "Begin forwarded message:\n"
        "From: NCB <alerts@jncb.com>\n"
        "Date: 09/02/2026\n"
        "Subject: Purchase Alert\n"
        "To: me@example.com\n\n"
        "Amount: JMD 2,550.00\n"
        "Transaction Date: 06/08/2026\n"
    )
    prepared = content_preparer.prepare(_parsed(text=body))
    assert "Date: 09/02/2026" not in prepared  # forward header stripped
    f = G.extract(prepared)
    assert f["transaction_date"].value == date(2026, 8, 6)  # not 9 Feb


def test_stub_text_part_falls_through_to_html_content():
    """A near-empty text part ('FYI') must not starve extraction — the real alert
    in the HTML part is used instead."""
    parsed = _parsed(
        text="FYI",
        html="<p>Amount: JMD 4,200.00</p><p>Merchant: FONTANA PHARMACY</p>",
    )
    prepared = content_preparer.prepare(parsed)
    f = G.extract(prepared)
    assert f["amount"].value == Decimal("4200.00")
    assert f["merchant"].value == "FONTANA PHARMACY"


def test_rich_text_part_is_not_overridden_by_html():
    """When the text part already carries the full alert, it is kept as-is."""
    text = (
        "National Commercial Bank purchase alert.\n"
        "Amount: JMD 999.00\n"
        "Merchant: SAMPLE STORE KINGSTON\n"
        "Transaction Date: 06/08/2026\n"
        "Card Number Ending: 4821\n"
        "Thank you for banking with us.\n"
    )
    parsed = _parsed(text=text, html="<p>garbage $1.00</p>")
    prepared = content_preparer.prepare(parsed)
    f = G.extract(prepared)
    assert f["amount"].value == Decimal("999.00")


def test_footer_marketing_lines_are_dropped():
    body = (
        "Amount: JMD 100.00\n"
        "Merchant: MEGAMART\n"
        "To unsubscribe from these alerts click here.\n"
    )
    prepared = content_preparer.prepare(_parsed(text=body))
    assert "unsubscribe" not in prepared.lower()


def test_html_fallback_is_redos_safe_on_unclosed_tags():
    """The regex HTML fallback must terminate quickly even on pathological input
    (many unclosed <script>), never hang."""
    hostile = "<script>" * 5000 + "Amount: JMD 5.00"
    parsed = _parsed(text="", html=hostile)
    # completes without catastrophic backtracking; assertion is that it returns
    prepared = content_preparer.prepare(parsed)
    assert isinstance(prepared, str)
