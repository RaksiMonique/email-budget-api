"""Sender-agnostic regex extraction. Amounts are Decimal, never float."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from app.extraction.models import Field

_SYMBOL_CCY = {"$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY"}

_AMOUNT_PATTERNS = [
    re.compile(r"([$£€¥])\s?([\d,]+\.\d{2})"),
    re.compile(r"\b(USD|JMD|EUR|GBP|CAD|AUD)\s?([\d,]+\.\d{2})", re.I),
    re.compile(r"([\d,]+\.\d{2})\s?(USD|JMD|EUR|GBP|CAD|AUD)\b", re.I),
]

_DATE_PATTERNS = [
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    re.compile(r"\b([A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4})\b"),
    re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b"),
]

_CARD_PATTERNS = [
    re.compile(r"ending in\s*\(?\.*(\d{4})\)?", re.I),
    re.compile(r"\(\.\.\.(\d{4})\)"),
    re.compile(r"[x\*]{2,}\s?(\d{4})"),
]

_NUM = re.compile(r"^[\d,]+\.\d{2}$")
_DATE_FORMATS = ["%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%m/%d/%Y"]

try:
    import dateparser as _dateparser  # type: ignore

    def _parse_date(raw: str) -> date | None:
        dt = _dateparser.parse(raw)
        return dt.date() if dt else None

except Exception:  # pragma: no cover - fallback when dateparser is not installed
    from datetime import datetime

    def _parse_date(raw: str) -> date | None:
        cleaned = raw.replace(".", "").strip()
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(cleaned, fmt).date()
            except ValueError:
                continue
        return None


def extract(text: str) -> dict[str, Field]:
    fields: dict[str, Field] = {}

    amount, currency, snippet = _amount(text)
    if amount is not None:
        fields["amount"] = Field(amount, "regex", snippet)
        if currency:
            fields["currency"] = Field(currency, "regex", snippet)

    for pat in _DATE_PATTERNS:
        if (m := pat.search(text)) and (d := _parse_date(m.group(1))):
            fields["transaction_date"] = Field(d, "regex", m.group(0))
            break

    for pat in _CARD_PATTERNS:
        if m := pat.search(text):
            fields["card_last4"] = Field(m.group(1), "regex", m.group(0))
            break

    return fields


def _amount(text: str) -> tuple[Decimal | None, str | None, str | None]:
    for pat in _AMOUNT_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        groups = m.groups()
        num = next((g for g in groups if g and _NUM.match(g)), None)
        if num is None:
            continue
        cur_token = next((g for g in groups if g and g != num), None)
        try:
            value = Decimal(num.replace(",", ""))
        except InvalidOperation:
            continue
        currency = _SYMBOL_CCY.get(cur_token, cur_token.upper()) if cur_token else None
        return value, currency, m.group(0)
    return None, None, None
