"""Sender-agnostic extraction. Amounts are Decimal, never float.

Two strategies (a per-sender template still wins over both — this is the generic
middle tier that lets most "labeled" bank alerts parse with NO template):

  1. LABEL — common field labels (Amount, Merchant/Payee, Date, Card ending)
     with the adjacent value (inline `Label: value` or label-above-value). This
     is the ONLY way to get a MERCHANT without a template, and — for amounts —
     an explicit `Amount:` label is preferred over the first currency match so a
     `Fee`/`Balance` line can't win.
  2. SHAPE — syntactic signatures: currency symbol/code + number, date-shaped
     strings, card-suffix phrases.

Locale: dates are parsed **day-first (DMY)** — the Jamaican/UK convention, so
`06/08/2026` is 6 Aug, not 8 Jun. We NEVER fabricate a value: a field we can't
find is left absent for the user to complete.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from app.extraction.models import Field

_SYMBOL_CCY = {"£": "GBP", "€": "EUR", "¥": "JPY"}
# localized dollar signs — J$ (Jamaica), TT$ (Trinidad), etc. must NOT read as USD
_DOLLAR_CCY = {
    "J$": "JMD", "TT$": "TTD", "BB$": "BBD", "US$": "USD", "CA$": "CAD",
    "C$": "CAD", "A$": "AUD", "EC$": "XCD", "$": "USD",
}
_CCY_CODES = "USD|JMD|EUR|GBP|CAD|AUD|TTD|BBD|XCD"
_NUM_CORE = r"[\d,]+(?:\.\d{2})?"  # decimals optional: "JMD 2550" and "JMD 2,550.00"

_AMOUNT_PATTERNS = [
    re.compile(rf"((?:J|TT|BB|US|CA|C|A|EC)?[$£€¥])\s?({_NUM_CORE})"),
    re.compile(rf"\b({_CCY_CODES})\s?({_NUM_CORE})", re.I),
    re.compile(rf"({_NUM_CORE})\s?({_CCY_CODES})\b", re.I),
]

_DATE_PATTERNS = [
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    re.compile(r"\b([A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4})\b"),
    re.compile(r"\b(\d{1,2}[/-][A-Za-z]{3,9}[/-]\d{4})\b"),  # 06/AUG/2026, 6-Aug-2026
    re.compile(r"\b(\d{1,2}[/.-]\d{1,2}[/.-]\d{4})\b"),      # 06/08/2026, 06-08-2026, 2026 dotted
    re.compile(r"\b(\d{4}[/.]\d{2}[/.]\d{2})\b"),
]

_CARD_PATTERNS = [
    re.compile(r"ending in\s*\(?\.*(\d{4})\)?", re.I),
    re.compile(r"\(\.\.\.(\d{4})\)"),
    re.compile(r"[x\*]{2,}\s?(\d{4})"),
]

_NUM = re.compile(r"^[\d,]+(?:\.\d{2})?$")
_DATE_FORMATS = [
    "%Y-%m-%d", "%d/%b/%Y", "%d-%b-%Y", "%d/%B/%Y", "%b %d, %Y", "%B %d, %Y",
    "%b %d %Y", "%B %d %Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%m/%d/%Y",
]


def _label(labels: str, value: str) -> re.Pattern:
    return re.compile(
        rf"(?im)^[ \t>*]*(?:{labels})[ \t]*:?[ \t]*\*?\n?[ \t>*]*({value})"
    )


_AMOUNT_LABEL = _label(r"amount charged|transaction amount|amount|total", r"[^\n]{1,40}")
_MERCHANT_LABEL = _label(
    r"merchant(?:\s+name)?|payee|retailer|vendor|business\s+name|description|location",
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
_LABEL_STOPWORDS = {
    "status", "amount", "date", "time", "merchant", "approved", "declined",
    "reference number", "card type", "card number ending", "transaction",
}


def _ccy(token: str) -> str:
    token = token.strip().upper()
    return _DOLLAR_CCY.get(token) or _SYMBOL_CCY.get(token) or token


try:
    import dateparser as _dateparser

    def _parse_date(raw: str) -> date | None:
        # year-first (ISO 2026-08-06, 2026.08.06) is unambiguous → YMD; anything
        # else is Jamaica/UK day-first so 06/08/2026 is 6 Aug, not 8 Jun
        order = "YMD" if re.match(r"\s*\d{4}[-/.]", raw) else "DMY"
        dt = _dateparser.parse(raw, settings={"DATE_ORDER": order})
        return dt.date() if dt else None

except Exception:  # pragma: no cover - fallback when dateparser is not installed
    from datetime import datetime

    def _parse_date(raw: str) -> date | None:
        for cand in (raw, raw.title()):  # title() normalizes "06/AUG/2026"
            cleaned = cand.strip()
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
    # prefer the value under an explicit Amount/Total label (so a Fee/Balance
    # line can't win); fall back to the first currency-shaped match anywhere.
    labelled = _AMOUNT_LABEL.search(text)
    scopes = ([labelled.group(1)] if labelled else []) + [text]
    for scope in scopes:
        for pat in _AMOUNT_PATTERNS:
            m = pat.search(scope)
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
            return value, (_ccy(cur_token) if cur_token else None), m.group(0)
    return None, None, None


def _date(text: str) -> tuple[date, str] | None:
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
