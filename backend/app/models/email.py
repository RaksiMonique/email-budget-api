from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ImportedEmail(Base):
    __tablename__ = "imported_emails"
    __table_args__ = (
        # alias-scoped message dedup (MVP; Phase 5 revisits user-scoped dedup).
        # Partial: message_id can legitimately be absent.
        Index(
            "uq_imported_emails_alias_message",
            "alias_hash",
            "message_id",
            unique=True,
            postgresql_where="message_id IS NOT NULL AND message_id != ''",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    alias_hash: Mapped[str] = mapped_column(String(64), index=True)
    # unique: the PRIMARY idempotency key for queue retries (always present on
    # ingest; nulled later by retention — PG unique indexes allow multiple NULLs)
    r2_key: Mapped[str | None] = mapped_column(String(512), unique=True)
    from_address: Mapped[str | None] = mapped_column(String(320))
    resolved_sender_domain: Mapped[str | None] = mapped_column(String(255))
    sender_source: Mapped[str | None] = mapped_column(String(16))  # dkim|header|body|none
    subject: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(String(998))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # received | processed | extraction_failed | non_financial |
    # forwarding_verification | error
    status: Mapped[str] = mapped_column(String(32), default="received", index=True)
    processing_errors: Mapped[str | None] = mapped_column(Text)
    # user-requested deletion (30-day grace): the maintenance loop purges the
    # R2 object and nulls r2_key once this passes (PLAN.md Phase 8)
    pending_deletion_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EmailClassification(Base):
    __tablename__ = "email_classifications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("imported_emails.id", ondelete="CASCADE"), index=True
    )
    is_financial: Mapped[bool]
    email_type: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    method: Mapped[str] = mapped_column(String(32))  # registry|subject|verification_sender|none


class ImportAuditLog(Base):
    __tablename__ = "import_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    action: Mapped[str] = mapped_column(String(64))
    previous_status: Mapped[str | None] = mapped_column(String(32))
    new_status: Mapped[str | None] = mapped_column(String(32))
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
