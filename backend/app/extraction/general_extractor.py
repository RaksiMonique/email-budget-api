"""Sender-agnostic extraction. Amounts are Decimal, never float.

Two strategies (a per-sender template still wins over both — this is the generic
middle tier that lets most "labeled" bank alerts parse with NO template):

  1. SHAPE — syntactic signatures: currency symbol/code + number, date-shaped
     strings, card-suffix phrases.
  2. LABEL — common field labels (Merchant/Payee, Amount, Date, Card ending)
     with the adjacent value (inline `Label: value` or label-above-value). This
     is the ONLY way to get a MERCHANT without a template — a merchant name has
     no shape of its own — and it makes NCB-style labeled alerts work generically.

We NEVER fabricate a value: a field we can't find is left absent for the user to
complete, never guessed.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from app.extraction.models import Field

_SYMBOL_CCY = {"$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY"}
_CCY_CODES = "USD|JMD|EUR|GBP|CAD|AUD|TTD|BBD|XCD"

_AMOUNT_PATTERNS = [
    re.compile(r"([$£€¥])\s?([\d,]+\.\d{2})"),
    re.compile(rf"\b({_CCY_CODES})\s?([\d,]+\.\d{{2}})", re.I),
    re.compile(rf"([\d,]+\.\d{{2}})\s?({_CCY_CODES})\b", re.I),
]

_DATE_PATTERNS = [
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    re.compile(r"\b([A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4})\b"),
    re.compile(r"\b(\d{1,2}/[A-Za-z]{3,9}/\d{4})\b"),  # 06/AUG/2026 (Caribbean banks)
    re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b"),
]

_CARD_PATTERNS = [
    re.compile(r"ending in\s*\(?\.*(\d{4})\)?", re.I),
    re.compile(r"\(\.\.\.(\d{4})\)"),
    re.compile(r"[x\*]{2,}\s?(\d{4})"),
]

_NUM = re.compile(r"^[\d,]+\.\d{2}$")
_DATE_FORMATS = [
    "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
    "%m/%d/%Y", "%d/%b/%Y", "%d/%B/%Y",
]


def _label(labels: str, value: str) -> re.Pattern:
    """Match a field label at line start (tolerating quote/emphasis markers),
    with its value inline after a colon OR on the next line (label-above-value)."""
    return re.compile(
        rf"(?im)^[ \t>*]*(?:{labels})[ \t]*:?[ \t]*\*?\n?[ \t>*]*({value})"
    )


_MERCHANT_LABEL = _label(
    r"merchant(?:\s+name)?|payee|retailer|vendor|business\s+name",
    r"[^\n]{2,60}",
)
_DATE_LABEL = _label(
    r"transaction\s+date|posting\s+date|purchase\s+date|date",
    r"[0-9A-Za-z][^\n]{5,30}",
)
_CARD_LABEL = re.compile(
    r"(?im)^[ \t>*]*(?:card(?:\s+number)?\s+ending(?:\s+in)?|account\s+ending)"
    r"[ \t]*:?[ \t]*\*?\n?[ \t>*.]*(\d{4})"
)
# a captured merchant value that IS another field label means the label had no
# value beneath it — reject rather than mis-capture
_LABEL_STOPWORDS = {
    "status", "amount", "date", "time", "merchant", "approved", "declined",
    "reference number", "card type", "card number ending", "transaction",
}

try:
    import dateparser as _dateparser

    def _parse_date(raw: str) -> date | None:
        dt = _dateparser.parse(raw)
        return dt.date() if dt else None

except Exception:  # pragma: no cover - fallback when dateparser is not installed
    from datetime import datetime

    def _parse_date(raw: str) -> date | None:
        for cand in (raw, raw.title()):  # title() normalizes "06/AUG/2026"
            cleaned = cand.replace(".", "").strip()
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

    if (d := _date(text)) is not None:
        fields["transaction_date"] = Field(d[0], "regex", d[1])
    if (c := _card(text)) is not None:
        fields["card_last4"] = Field(c[0], "regex", c[1])
    if (m := _merchant(text)) is not None:
        fields["merchant"] = Field(m[0], "regex", m[1])

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


def _date(text: str) -> tuple[date, str] | None:
    # shape patterns first (anchored to a real date token), then a Date: label
    for pat in _DATE_PATTERNS:
        if (m := pat.search(text)) and (d := _parse_date(m.group(1))):
            return d, m.group(0)
    if (m := _DATE_LABEL.search(text)) and (d := _parse_date(m.group(1).strip())):
        return d, m.group(0)[:40]
    return None


def _card(text: str) -> tuple[str, str] | None:
    if m := _CARD_LABEL.search(text):
        return m.group(1), m.group(0)[:40]
    for pat in _CARD_PATTERNS:
        if m := pat.search(text):
            return m.group(1), m.group(0)
    return None


def _merchant(text: str) -> tuple[str, str] | None:
    m = _MERCHANT_LABEL.search(text)
    if not m:
        return None
    val = m.group(1).strip().strip(":").strip()
    if not val or _NUM.match(val) or val.lower() in _LABEL_STOPWORDS:
        return None
    return val, m.group(0)[:60]
