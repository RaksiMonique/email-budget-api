from datetime import date
from decimal import Decimal
from pathlib import Path

from app.extraction.models import EmailType, SenderSource, Status
from app.extraction.pipeline import run

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "synthetic"


def _run(name: str):
    return run((FIXTURES / name).read_bytes())


def test_chase_alert_end_to_end():
    r = _run("chase_alert.eml")

    # sender resolution prefers DKIM d=chase.com even though the mail was forwarded
    assert r.resolved_sender.domain == "chase.com"
    assert r.resolved_sender.source == SenderSource.DKIM

    assert r.classification.is_financial is True
    assert r.classification.email_type == EmailType.BANK_ALERT

    assert r.value("amount") == Decimal("45.99")
    assert r.value("currency") == "USD"
    assert r.value("transaction_date") == date(2026, 7, 12)
    assert r.value("card_last4") == "1234"

    # amount should come from the chase template, not the general regex fallback
    assert r.fields["amount"].method == "template"

    assert r.merchant_normalized == "Amazon"
    assert r.category_suggestion == "Shopping"

    assert r.status == Status.PENDING_REVIEW
    assert r.confidence_band == "high"
    assert r.extraction_confidence >= 0.85
    assert r.fingerprint is not None


def test_amount_is_decimal_never_float():
    r = _run("chase_alert.eml")
    assert isinstance(r.value("amount"), Decimal)


NEWSLETTER = b"""From: Weekly Digest <news@example.com>
To: abc123token@fintrack.raksimoni.com
Subject: Your weekly roundup of top stories
Date: Sun, 12 Jul 2026 09:00:00 +0000
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

Here are this week's most popular articles. Enjoy your Sunday reading!
"""


def test_non_financial_is_skipped():
    r = run(NEWSLETTER)
    assert r.classification.is_financial is False
    assert r.status == Status.NON_FINANCIAL
    assert r.fields == {}
