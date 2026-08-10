"""Per-field + overall confidence, and status routing.

Two routes only: `extraction_failed` (below floor or missing a required field) or
`pending_review`. The 0.85 line is a confidence *badge* on pending_review, not a
second destination. Auto-confirm is off for MVP (PLAN.md Phase 1).
"""
from __future__ import annotations

from app.extraction.models import REQUIRED_FIELDS, Field, Status

_METHOD_CONF = {"template": 0.97, "regex": 0.75, "default": 0.6}
_WEIGHTS = {"amount": 0.35, "transaction_date": 0.20, "merchant": 0.25, "currency": 0.10}
_OPTIONAL_BONUS = {"card_last4": 0.05}

HIGH_BAND = 0.85
FAIL_BELOW = 0.60


def field_confidences(fields: dict[str, Field]) -> dict[str, float]:
    keys = ("amount", "transaction_date", "merchant", "currency", "card_last4")
    return {
        k: (_METHOD_CONF.get(fields[k].method, 0.0) if k in fields else 0.0) for k in keys
    }


def score(fields: dict[str, Field]) -> tuple[float, dict[str, float]]:
    conf = field_confidences(fields)
    overall = sum(w * conf[k] for k, w in _WEIGHTS.items())
    overall += sum(b * conf[k] for k, b in _OPTIONAL_BONUS.items())
    return min(overall, 1.0), conf


def route(overall: float, fields: dict[str, Field]) -> tuple[Status, str]:
    """An **amount is the floor** for a usable extraction. With one, always route
    to `pending_review` — partial data the user completes (missing merchant/date
    left blank, never guessed) beats a hard failure, so a new bank with no
    template still lands as a reviewable transaction. The band signals how
    complete/confident it is; only a *missing amount* is a true failure.
    """
    if "amount" not in fields:
        return Status.EXTRACTION_FAILED, "n/a"
    complete = all(k in fields for k in REQUIRED_FIELDS)
    band = "high" if (complete and overall >= HIGH_BAND) else "low_confidence"
    return Status.PENDING_REVIEW, band
