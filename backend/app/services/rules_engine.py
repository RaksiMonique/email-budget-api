"""Merchant normalization + category suggestion (pure)."""
from __future__ import annotations

import re

from app.seed.merchant_rules import CATEGORY_MAP, MERCHANT_RULES

_TXN_ID_TAIL = re.compile(r"[\*#]\S+$")


def normalize_merchant(raw: str | None) -> tuple[str | None, str | None]:
    """Return (normalized_name, category). Category may be None if unknown."""
    if not raw:
        return None, None
    candidate = raw.strip()
    upper = candidate.upper()

    for order in ("starts_with", "contains", "exact", "regex"):
        for rule in MERCHANT_RULES:
            if rule["match_type"] != order:
                continue
            pat = rule["pattern"].upper()
            hit = (
                (order == "starts_with" and upper.startswith(pat))
                or (order == "contains" and pat in upper)
                or (order == "exact" and upper == pat)
                or (order == "regex" and re.search(rule["pattern"], candidate, re.I))
            )
            if hit:
                return rule["normalized"], rule.get("category")

    # generic cleanup fallback
    cleaned = _TXN_ID_TAIL.sub("", candidate).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    normalized = cleaned.title() if (cleaned.isupper() or cleaned.islower()) else cleaned
    return (normalized or None), CATEGORY_MAP.get(normalized)
