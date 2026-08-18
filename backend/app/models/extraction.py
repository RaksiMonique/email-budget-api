from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ExtractionResult(Base):
    __tablename__ = "extraction_results"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("imported_emails.id", ondelete="CASCADE"), index=True
    )
    external_user_id: Mapped[str] = mapped_column(String(255), index=True)
    alias_hash: Mapped[str] = mapped_column(String(64))

    # money is Decimal end-to-end; serialized as strings in every payload
    amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 4))
    currency: Mapped[str | None] = mapped_column(CHAR(3))
    merchant_raw: Mapped[str | None] = mapped_column(Text)
    merchant_normalized: Mapped[str | None] = mapped_column(String(255))
    category_suggestion: Mapped[str | None] = mapped_column(String(100))
    transaction_date: Mapped[date | None] = mapped_column(Date)
    card_last4: Mapped[str | None] = mapped_column(CHAR(4))

    extraction_confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), default=Decimal("0"))
    confidence_band: Mapped[str] = mapped_column(String(16), default="n/a")  # high|low_confidence|n/a
    field_confidences: Mapped[dict | None] = mapped_column(JSONB)
    method: Mapped[str | None] = mapped_column(String(16))  # template|regex|mixed

    # pending_review | confirmed | dismissed | extraction_failed | duplicate_suppressed
    status: Mapped[str] = mapped_column(String(32), index=True)
    category_confirmed: Mapped[str | None] = mapped_column(String(100))
    dismissed_reason: Mapped[str | None] = mapped_column(Text)
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    duplicate_confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), default=Decimal("0"))
    # transaction direction / status (debit is the safe default — see
    # general_extractor.transaction_flags). amount stays a positive magnitude;
    # direction carries the sign so existing consumers don't break.
    direction: Mapped[str] = mapped_column(String(8), server_default="debit", default="debit")
    is_probable_refund: Mapped[bool] = mapped_column(
        Boolean, server_default=false(), default=False
    )
    is_declined: Mapped[bool] = mapped_column(
        Boolean, server_default=false(), default=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ExtractionSnippet(Base):
    __tablename__ = "extraction_snippets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("extraction_results.id", ondelete="CASCADE"), index=True
    )
    raw_snippet: Mapped[str] = mapped_column(Text)
    snippet_type: Mapped[str] = mapped_column(String(50))  # amount|merchant|date|full|...


class ExtractionTemplate(Base):
    __tablename__ = "extraction_templates"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    sender_domain: Mapped[str] = mapped_column(String(255), unique=True)
    patterns: Mapped[dict] = mapped_column(JSONB)  # {field: [regex, ...]}
    email_type: Mapped[str | None] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer, default=1)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DuplicateMatch(Base):
    __tablename__ = "duplicate_matches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("extraction_results.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    candidate_type: Mapped[str] = mapped_column(String(32))  # exact_fingerprint|fuzzy(Phase 2)
    duplicate_confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
