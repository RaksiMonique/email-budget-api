"""Rule-based financial classification (pure — no DB in Phase 1).

Phase 4 adds a persisting variant; the decision logic stays here.
"""
from __future__ import annotations

import re

from app.extraction.models import Classification, EmailType, ResolvedSender
from app.seed.financial_senders import (
    FINANCIAL_SENDER_REGISTRY,
    FINANCIAL_SUBJECT_PATTERNS,
)

_SUBJECT_RE = re.compile("|".join(FINANCIAL_SUBJECT_PATTERNS), re.I)


def classify(sender: ResolvedSender, subject: str) -> Classification:
    etype = FINANCIAL_SENDER_REGISTRY.get(sender.domain or "")
    if etype is not None:
        return Classification(True, etype, 0.95, "registry")
    if subject and _SUBJECT_RE.search(subject):
        return Classification(True, EmailType.UNKNOWN, 0.60, "subject")
    return Classification(False, EmailType.NON_FINANCIAL, 0.50, "none")
