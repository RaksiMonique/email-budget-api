"""Seed data for classification.

In MVP this seeds the `financial_sender_registry` table; Phase 1 uses it in-memory.
"""
from __future__ import annotations

from app.extraction.models import EmailType

# resolved (registered) sender domain -> email type
FINANCIAL_SENDER_REGISTRY: dict[str, EmailType] = {
    # banks (P0 — highest signal)
    "chase.com": EmailType.BANK_ALERT,
    "bankofamerica.com": EmailType.BANK_ALERT,
    "wellsfargo.com": EmailType.BANK_ALERT,
    # payments
    "paypal.com": EmailType.PAYMENT,
    "venmo.com": EmailType.PAYMENT,
    "stripe.com": EmailType.RECEIPT,
    # merchants / subscriptions
    "amazon.com": EmailType.RECEIPT,
    "apple.com": EmailType.RECEIPT,
    "uber.com": EmailType.RECEIPT,
    "netflix.com": EmailType.SUBSCRIPTION,
    "spotify.com": EmailType.SUBSCRIPTION,
}

# subject keywords suggesting a financial email even from an unknown sender
FINANCIAL_SUBJECT_PATTERNS: tuple[str, ...] = (
    r"receipt", r"invoice", r"payment", r"charged", r"purchase", r"order",
    r"transaction", r"statement", r"alert", r"refund", r"credit", r"debit",
    r"withdrawal", r"deposit", r"subscription", r"renewal", r"bill",
    r"confirmation",
)
