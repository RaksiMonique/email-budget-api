"""Per-sender extraction templates (Phase 1 in-memory; later `extraction_templates`).

PROVISIONAL: these regexes are seeded from expected formats and MUST be validated
and expanded against the real .eml corpus (PLAN.md Phase 1). Start with banks.
"""
from __future__ import annotations

from app.extraction.models import EmailType

# domain -> {"email_type": EmailType, "fields": {field: [regex, ...]}}
TEMPLATES: dict[str, dict] = {
    "chase.com": {
        "email_type": EmailType.BANK_ALERT,
        "fields": {
            "amount": [
                r"\$\s?([\d,]+\.\d{2})\s+(?:transaction|purchase|payment)",
                r"(?:amount|total)\s*:?\s*\$\s?([\d,]+\.\d{2})",
                r"\$\s?([\d,]+\.\d{2})",
            ],
            "merchant": [
                r"(?:transaction|purchase|payment)\s+(?:with|at|to)\s+([A-Z0-9][^\n.]+?)(?:\s+on\b|[.\n])",
                r"merchant\s*:?\s*([^\n]+)",
            ],
            "transaction_date": [
                r"on\s+([A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4})",
                r"date\s*:?\s*([A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4})",
                r"on\s+(\d{1,2}/\d{1,2}/\d{4})",
            ],
            "card_last4": [
                r"ending in\s*\(?\.*(\d{4})\)?",
                r"\(\.\.\.(\d{4})\)",
                r"[x\*]{2,}\s?(\d{4})",
            ],
        },
    },
}
