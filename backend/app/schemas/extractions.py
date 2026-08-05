from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class _DecimalAsString(BaseModel):
    """Money and confidences serialize as strings — never JSON floats."""

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("amount", "extraction_confidence", "duplicate_confidence", check_fields=False)
    def _dec(self, v: Decimal | None) -> str | None:
        # normalize() strips DB Numeric scale-padding ("45.9900" vs "45.99") so
        # the same value serializes identically on every code path
        return None if v is None else format(v.normalize(), "f")


class ExtractionSummary(_DecimalAsString):
    id: uuid.UUID
    email_id: uuid.UUID
    external_user_id: str
    amount: Decimal | None
    currency: str | None
    merchant_normalized: str | None
    category_suggestion: str | None
    transaction_date: date | None
    extraction_confidence: Decimal
    confidence_band: str
    duplicate_confidence: Decimal
    status: str
    created_at: datetime


class DuplicateMatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    candidate_id: uuid.UUID
    candidate_type: str
    duplicate_confidence: Decimal

    @field_serializer("duplicate_confidence")
    def _dec(self, v: Decimal) -> str:
        return format(v.normalize(), "f")


class ExtractionDetail(ExtractionSummary):
    alias_hash: str
    merchant_raw: str | None
    card_last4: str | None
    category_confirmed: str | None
    dismissed_reason: str | None
    field_confidences: dict | None
    method: str | None
    fingerprint: str | None
    duplicate_matches: list[DuplicateMatchOut] = []


class SnippetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    snippet_type: str
    raw_snippet: str


class ExtractionPreview(BaseModel):
    extraction: ExtractionDetail
    snippets: list[SnippetOut]


class ExtractionPage(BaseModel):
    items: list[ExtractionSummary]
    total: int
    limit: int
    offset: int


class ConfirmBody(BaseModel):
    category: str | None = Field(default=None, max_length=100)


class DismissBody(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class CategoryFeedbackBody(BaseModel):
    extraction_id: uuid.UUID | None = None
    merchant_normalized: str = Field(min_length=1, max_length=255)
    category_confirmed: str = Field(min_length=1, max_length=100)


class WebhookConfigBody(BaseModel):
    webhook_url: str = Field(min_length=8, max_length=1024, pattern=r"^https?://")
    webhook_secret: str = Field(min_length=16, max_length=512)
