"""Regression tests for the general (template-free) extractor — locking in the
fixes the stress-test matrix surfaced (currency, no-decimals, labeled-amount
priority, day-first dates, merchant label synonyms)."""
from datetime import date
from decimal import Decimal

import pytest

from app.extraction import general_extractor as G


@pytest.mark.parametrize(
    "body, ccy, amt",
    [
        ("Amount: J$2,550.00", "JMD", Decimal("2550.00")),   # J$ -> JMD, NOT USD
        ("Amount: JMD 2550", "JMD", Decimal("2550")),        # decimals optional
        ("Amount: TTD 100.00", "TTD", Decimal("100.00")),
        ("Amount: 2,550.00 JMD", "JMD", Decimal("2550.00")),
    ],
)
def test_amount_and_currency(body, ccy, amt):
    f = G.extract(body)
    assert f["amount"].value == amt
    assert f["currency"].value == ccy


def test_bare_dollar_is_ambiguous_currency():
    # a bare "$" is ambiguous (JMD in JM, USD in the US) — the extractor leaves
    # currency UNKNOWN; the pipeline fills it from the sender's default currency
    f = G.extract("Amount: $2,550.00")
    assert f["amount"].value == Decimal("2550.00")
    assert "currency" not in f


def test_amount_prefers_labeled_over_fee_and_balance():
    f = G.extract("Fee: JMD 50.00\nAmount: JMD 2,550.00\nBalance: JMD 90,000.00")
    assert f["amount"].value == Decimal("2550.00")  # the Amount, not the Fee


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Date: 06/08/2026", date(2026, 8, 6)),   # day-first (Jamaica) -> 6 Aug
        ("Date: 06-08-2026", date(2026, 8, 6)),
        ("Date: 2026-08-06", date(2026, 8, 6)),   # ISO stays year-first
        ("Date: 06/AUG/2026", date(2026, 8, 6)),
        ("Date: 6 August 2026", date(2026, 8, 6)),
        ("Date: Aug 6, 2026", date(2026, 8, 6)),
    ],
)
def test_dates_are_day_first_except_iso(raw, expected):
    f = G.extract("Amount: $1.00\n" + raw)
    assert f["transaction_date"].value == expected


@pytest.mark.parametrize("label", ["Merchant", "Payee", "Description", "Location", "Vendor", "Retailer"])
def test_merchant_label_synonyms(label):
    f = G.extract(f"Amount: JMD 1.00\n{label}: TASTEE LIGUANEA")
    assert f["merchant"].value == "TASTEE LIGUANEA"


def test_blank_merchant_label_is_not_captured():
    f = G.extract("Merchant:\nStatus\nAmount: JMD 1.00")
    assert "merchant" not in f  # never grab the next label as a merchant


def test_phone_number_is_not_an_amount():
    f = G.extract("Call 1-876-555-1234.00 now. Amount: JMD 500.00")
    assert f["amount"].value == Decimal("500.00")


def test_bare_amount_under_label_no_currency():
    # First Global Bank style: "Amount: 670.00" — bare number, no currency code.
    # The explicit label makes it the amount; currency stays UNKNOWN (not guessed).
    f = G.extract("Merchant: CHINAMAX RESTAURANT\nAmount: 670.00\nStatus: Approved")
    assert f["amount"].value == Decimal("670.00")
    assert "currency" not in f


@pytest.mark.parametrize(
    "text, direction, refund, declined",
    [
        ("Amount: JMD 500.00\nMerchant: STORE", "debit", False, False),      # plain purchase
        ("A refund of JMD 500.00 was processed", "credit", True, False),     # refund word
        ("Reversal of JMD 500.00 to your account", "credit", True, False),   # reversal word
        ("Transaction Type: Refund\nAmount: JMD 500.00", "credit", True, False),  # Type field
        ("Transaction Type: Credit\nAmount: JMD 500.00", "credit", False, False), # credit, not refund
        ("Deposit of JMD 500.00 received", "credit", False, False),          # deposit
        ("Your credit card purchase of JMD 500.00", "debit", False, False),  # GUARD: bare "credit"
        ("Card Type: Credit Card\nAmount: JMD 500.00", "debit", False, False),  # GUARD: "credit card"
        ("Amount: JMD 500.00\nStatus: DECLINED", "debit", False, True),      # declined charge
        ("A refund was declined", "credit", True, True),                     # refund + declined
    ],
)
def test_transaction_flags(text, direction, refund, declined):
    # (direction, is_probable_refund, is_declined) — debit is the safe default,
    # a bare "credit card" must never read as a credit/refund
    assert G.transaction_flags(text) == (direction, refund, declined)
