"""Typed results passed between extraction pipeline stages (pure, cloud-free)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class SenderSource(str, Enum):
    DKIM = "dkim"
    HEADER = "header"
    BODY = "body"
    NONE = "none"


class EmailType(str, Enum):
    BANK_ALERT = "bank_alert"
    RECEIPT = "receipt"
    SUBSCRIPTION = "subscription"
    PAYMENT = "payment"
    UNKNOWN = "unknown"
    NON_FINANCIAL = "non_financial"


class Status(str, Enum):
    PENDING_REVIEW = "pending_review"
    EXTRACTION_FAILED = "extraction_failed"
    NON_FINANCIAL = "non_financial"


# extraction field keys
REQUIRED_FIELDS = ("amount", "merchant", "transaction_date")
ALL_FIELDS = ("amount", "currency", "merchant", "transaction_date", "card_last4")


@dataclass
class ParsedEmail:
    subject: str
    from_header: str
    dkim_signatures: list[str]
    headers: dict[str, str]
    text_body: str
    html_body: str


@dataclass
class ResolvedSender:
    domain: Optional[str]
    source: SenderSource
    confidence: float


@dataclass
class Classification:
    is_financial: bool
    email_type: EmailType
    confidence: float
    method: str  # registry | subject | none


@dataclass
class Field:
    value: Any
    method: str  # template | regex
    snippet: Optional[str] = None


@dataclass
class ExtractionResult:
    resolved_sender: ResolvedSender
    classification: Classification
    fields: dict[str, Field]
    merchant_normalized: Optional[str]
    category_suggestion: Optional[str]
    field_confidences: dict[str, float]
    extraction_confidence: float
    confidence_band: str  # high | low_confidence | n/a
    status: Status
    fingerprint: Optional[str] = None

    def value(self, key: str) -> Any:
        """Convenience accessor for a field's extracted value (or None)."""
        f = self.fields.get(key)
        return f.value if f else None
