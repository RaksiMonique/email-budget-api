"""Per-sender template extraction against prepared content."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from app.extraction.general_extractor import _parse_date  # reuse date parsing
from app.extraction.models import EmailType, Field
from app.seed.templates import TEMPLATES


def email_type_for(domain: str | None) -> EmailType | None:
    tpl = TEMPLATES.get(domain or "")
    return tpl["email_type"] if tpl else None


def extract(domain: str | None, text: str) -> dict[str, Field]:
    tpl = TEMPLATES.get(domain or "")
    if not tpl:
        return {}
    out: dict[str, Field] = {}
    for field_name, patterns in tpl["fields"].items():
        for raw_pat in patterns:
            m = re.search(raw_pat, text, re.I)
            if not m:
                continue
            value = _coerce(field_name, m.group(1).strip())
            if value is not None:
                out[field_name] = Field(value, "template", m.group(0))
                break
    return out


def _coerce(field_name: str, raw: str):
    if field_name == "amount":
        try:
            return Decimal(raw.replace(",", ""))
        except InvalidOperation:
            return None
    if field_name == "transaction_date":
        return _parse_date(raw)
    return raw
