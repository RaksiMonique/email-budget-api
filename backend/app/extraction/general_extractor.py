"""Sender-agnostic extraction. Amounts are Decimal, never float.

Two strategies (a per-sender template still wins over both — this is the generic
middle tier that lets most "labeled" bank alerts parse with NO template):

  1. LABEL — common field labels (Amount, Merchant/Payee, Transaction Date,
     Card ending) with the adjacent value. A label must be followed by ":" or a
     newline (label-above-value), so narrative prefixes like "Amount Available"
     or "Total for this purchase" don't hijack a field. This is the ONLY way to
     get a MERCHANT without a template, and the labeled Amount/Transaction-Date
     win over stray shape matches (a Balance line, a forward's Date: header).
  2. SHAPE — syntactic signatures: currency symbol/code + number, date-shaped
     strings, card-suffix phrases.

Locale: dates are day-first (DMY) — 06/08/2026 is 6 Aug, not 8 Jun. We NEVER
fabricate a value: unfound fields are left absent, and merchant values that look
like another label / a date / an amount / a greeting are rejected, not guessed.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from app.extraction.models import Field

_SYMBOL_CCY = {"£": "GBP", "€": "EUR", "¥": "JPY"}
# localized dollar signs. A BARE "$" is intentionally absent — ambiguous (JMD in
# JM, USD in the US) — so it is left unknown and filled from the sender default.
_DOLLAR_CCY = {
    "J$": "JMD", "TT$": "TTD", "BB$": "BBD", "US$": "USD", "CA$": "CAD",
    "C$": "CAD", "A$": "AUD", "EC$": "XCD",
}
_CCY_CODES = "USD|JMD|EUR|GBP|CAD|AUD|TTD|BBD|XCD"
_NUM_CORE = r"[\d,]+(?:\.\d{2})?"

_AMOUNT_PATTERNS = [
    # \b guards the OPTIONAL letter prefix so "VISA$100" can't read "A$" -> AUD,
    # while a bare "$" (at string start or after a space, no word boundary) still
    # matches — the \b is inside the optional group, not before the symbol.
    re.compile(rf"((?:\b(?:J|TT|BB|US|CA|C|A|EC))?[$£€¥])\s?({_NUM_CORE})"),
    re.compile(rf"\b({_CCY_CODES})\s?({_NUM_CORE})", re.I),
    re.compile(rf"({_NUM_CORE})\s?({_CCY_CODES})\b", re.I),
]

_DATE_PATTERNS = [
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    re.compile(r"\b([A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4})\b"),
    re.compile(r"\b(\d{1,2}[/-][A-Za-z]{3,9}[/-]\d{4})\b"),
    re.compile(r"\b(\d{1,2}[/.-]\d{1,2}[/.-]\d{4})\b"),
    re.compile(r"\b(\d{4}[/.]\d{2}[/.]\d{2})\b"),
]

_CARD_PATTERNS = [
    re.compile(r"ending in\s*\(?\.*(\d{4})\)?", re.I),
    re.compile(r"\(\.\.\.(\d{4})\)"),
    re.compile(r"[x\*]{2,}\s?(\d{4})"),
]

_NUM = re.compile(r"^[\d,]+(?:\.\d{2})?$")
# a bare amount must be at the START of the labeled value ("Amount: 670.00"), not
# any digit run later in the line (a card last-4 / reference number in prose)
_BARE_NUM = re.compile(r"^\s*(\d[\d,]*(?:\.\d{2})?)\b")
_DATE_FORMATS = [
    "%Y-%m-%d", "%d/%b/%Y", "%d-%b-%Y", "%d/%B/%Y", "%b %d, %Y", "%B %d, %Y",
    "%b %d %Y", "%B %d %Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%m/%d/%Y",
]


def _label(labels: str, value: str) -> re.Pattern:
    # label must be followed by ":" (inline) or a newline (label-above-value) —
    # so "Amount Available"/"Total Rewards" narrative prefixes never match
    return re.compile(
        rf"(?im)^[ \t>*]*(?:{labels})[ \t]*(?::[ \t]*|\n[ \t>*]*)({value})"
    )


_AMOUNT_LABEL = _label(r"amount charged|transaction amount|amount|total", r"[^\n]{0,80}")
_MERCHANT_LABEL = _label(
    r"merchant(?:\s+name)?|payee|retailer|vendor|business\s+name|description|location",
    r"[^\n]{2,60}",
)
# a SPECIFIC transaction-date label is preferred over any date-shaped string (a
# forward's "Date:" header, a "due date") — bare "Date:" is only a last resort
_DATE_LABEL_SPECIFIC = _label(
    r"transaction\s+date|posting\s+date|purchase\s+date|txn\s+date|value\s+date",
    r"[0-9A-Za-z][^\n]{5,30}",
)
_DATE_LABEL_ANY = _label(r"date", r"[0-9A-Za-z][^\n]{5,30}")
_CARD_LABEL = re.compile(
    r"(?im)^[ \t>*]*(?:card(?:\s+number)?\s+ending(?:\s+in)?|account\s+ending)"
    r"[ \t]*(?::[ \t]*|\n[ \t>*]*)[*.\s]*(\d{4})"
)

_LABEL_STOPWORDS = {
    "status", "amount", "date", "time", "merchant", "approved", "declined",
    "reference number", "card type", "card number ending", "transaction",
}
# a merchant value must not look like another field: a date, an amount, or a
# greeting — those are captured mistakes, not merchants
_DATE_ISH = re.compile(r"\d{1,2}[/.\-][A-Za-z0-9]{2,9}[/.\-]\d{2,4}")
_MONEY_ISH = re.compile(rf"[$£€¥]|\b(?:{_CCY_CODES})\b", re.I)


def _ccy(token: str) -> str | None:
    token = token.strip().upper()
    if token == "$":
        return None  # bare dollar is ambiguous — the sender's default decides
    return _DOLLAR_CCY.get(token) or _SYMBOL_CCY.get(token) or token


try:
    import dateparser as _dateparser

    def _parse_date(raw: str) -> date | None:
        order = "YMD" if re.match(r"\s*\d{4}[-/.]", raw) else "DMY"
        dt = _dateparser.parse(raw, settings={"DATE_ORDER": order})
        return dt.date() if dt else None

except Exception:  # pragma: no cover - fallback when dateparser is not installed
    from datetime import datetime

    def _parse_date(raw: str) -> date | None:
        for cand in (raw, raw.title()):
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


def _match_amount(pat: re.Pattern, scope: str):
    m = pat.search(scope)
    if not m:
        return None
    groups = m.groups()
    num = next((g for g in groups if g and _NUM.match(g)), None)
    if num is None:
        return None
    cur = next((g for g in groups if g and g != num), None)
    try:
        value = Decimal(num.replace(",", ""))
    except InvalidOperation:
        return None
    return value, (_ccy(cur) if cur else None), m.group(0)


def _amount(text: str) -> tuple[Decimal | None, str | None, str | None]:
    # The labeled Amount ALWAYS wins (currency-coded or bare) over any stray
    # currency match elsewhere (a Balance/Fee/rewards line).
    labelled = _AMOUNT_LABEL.search(text)
    if labelled and (val := labelled.group(1)):
        for pat in _AMOUNT_PATTERNS:
            if (r := _match_amount(pat, val)) is not None:
                return r
        if m := _BARE_NUM.search(val):  # "Amount: 670.00" with no currency
            try:
                return Decimal(m.group(1).replace(",", "")), None, f"Amount: {m.group(1)}"
            except InvalidOperation:
                pass
    for pat in _AMOUNT_PATTERNS:
        if (r := _match_amount(pat, text)) is not None:
            return r
    return None, None, None


def _date(text: str) -> tuple[date, str] | None:
    # 1. specific transaction-date label (beats a forward "Date:" header / due date)
    if (m := _DATE_LABEL_SPECIFIC.search(text)) and (d := _parse_date(m.group(1).strip())):
        return d, m.group(0)[:40]
    # 2. shape patterns
    for pat in _DATE_PATTERNS:
        if (m := pat.search(text)) and (d := _parse_date(m.group(1))):
            return d, m.group(0)
    # 3. bare "Date:" label, last resort
    if (m := _DATE_LABEL_ANY.search(text)) and (d := _parse_date(m.group(1).strip())):
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
    val = m.group(1).strip()
    low = val.lower()
    if (
        not val
        or _NUM.match(val)
        or ":" in val  # captured the next label line (blank merchant field)
        or low in _LABEL_STOPWORDS
        or low.startswith(("dear ", "dear\t", "hello", "hi "))
        or _DATE_ISH.search(val)
        or _MONEY_ISH.search(val)
    ):
        return None
    return val, m.group(0)[:60]
