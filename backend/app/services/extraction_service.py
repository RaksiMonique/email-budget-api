"""Persist a pure-pipeline ExtractionResult + enqueue outbox events.

The pure pipeline (app/extraction/pipeline.py) stays cloud-free; this service
is the only layer that touches the DB. Called inside the /internal webhook's
single transaction — the caller commits.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.extraction.models import ExtractionResult as PipelineResult
from app.extraction.models import Status
from app.models import (
    EmailClassification,
    ExtractionResult,
    ExtractionSnippet,
    ImportedEmail,
    WebhookOutbox,
)
from app.services import duplicate_service

# pipeline status -> imported_emails.status
_EMAIL_STATUS = {
    Status.PENDING_REVIEW: "processed",
    Status.EXTRACTION_FAILED: "extraction_failed",
    Status.NON_FINANCIAL: "non_financial",
    Status.FORWARDING_VERIFICATION: "forwarding_verification",
}

# Numeric(15,4) holds < 10^11 in the integer part. An attacker-deliverable
# "USD 999999999999.99" must degrade to extraction_failed, never DataError→
# 500→retry→DLQ (poison message).
_MAX_AMOUNT = Decimal("1e11")


def _amount_str(value: Decimal | None) -> str | None:
    # canonical form (no scale padding) — matches the API schema serializers
    return None if value is None else format(value.normalize(), "f")


def apply_result_fields(row: ExtractionResult, result: PipelineResult) -> str:
    """Map pipeline output onto an ExtractionResult row (new or reprocessed).

    Single source of truth for the field mapping — used by both initial
    persistence and /reprocess so they can never drift. Returns the final
    status value (out-of-range amounts degrade to extraction_failed here).
    """
    amount = result.value("amount")
    status_value = result.status.value  # pending_review | extraction_failed
    if amount is not None and abs(amount) >= _MAX_AMOUNT:
        amount = None  # out of column range — required field now missing
        status_value = Status.EXTRACTION_FAILED.value

    methods = {f.method for f in result.fields.values()}
    row.amount = amount
    row.currency = result.value("currency")
    row.merchant_raw = result.value("merchant")
    row.merchant_normalized = result.merchant_normalized
    row.category_suggestion = result.category_suggestion
    row.transaction_date = result.value("transaction_date")
    row.card_last4 = result.value("card_last4")
    row.extraction_confidence = Decimal(str(result.extraction_confidence))
    row.confidence_band = result.confidence_band
    row.field_confidences = result.field_confidences
    row.method = "mixed" if len(methods) > 1 else next(iter(methods), None)
    row.status = status_value
    row.fingerprint = result.fingerprint if amount is not None else None
    return status_value


def _extraction_event_payload(row: ExtractionResult, email: ImportedEmail) -> dict:
    return {
        "extraction_id": str(row.id),
        "email_id": str(email.id),
        "external_user_id": row.external_user_id,
        "alias_hash": email.alias_hash,
        # money as strings — never JSON floats
        "amount": _amount_str(row.amount),
        "currency": row.currency,
        "merchant": row.merchant_normalized or row.merchant_raw,
        "category_suggestion": row.category_suggestion,
        "transaction_date": (
            row.transaction_date.isoformat() if row.transaction_date else None
        ),
        "extraction_confidence": str(row.extraction_confidence),
        "confidence_band": row.confidence_band,
        # flag-only dedup: "1" means an exact-fingerprint match exists and the
        # UI should show a possible-duplicate badge; the row is still live
        "duplicate_confidence": format(row.duplicate_confidence.normalize(), "f"),
        "status": row.status,
    }


async def persist_result(
    db: AsyncSession,
    email: ImportedEmail,
    external_user_id: str,
    result: PipelineResult,
) -> ExtractionResult | None:
    """Store classification/extraction rows and outbox events. Returns the
    ExtractionResult row when one is created (financial emails only)."""

    email.resolved_sender_domain = result.resolved_sender.domain
    email.sender_source = result.resolved_sender.source.value
    email.status = _EMAIL_STATUS[result.status]

    db.add(
        EmailClassification(
            email_id=email.id,
            is_financial=result.classification.is_financial,
            email_type=result.classification.email_type.value,
            confidence=Decimal(str(result.classification.confidence)),
            method=result.classification.method,
        )
    )

    if result.status == Status.FORWARDING_VERIFICATION:
        db.add(
            WebhookOutbox(
                event_type="forwarding.verification",
                payload_json={
                    "alias_hash": email.alias_hash,
                    "provider": "gmail",
                    "code": result.value("verification_code"),
                    "confirmation_url": result.value("confirmation_url"),
                    "received_at": email.received_at.isoformat() if email.received_at else None,
                },
            )
        )
        return None

    if result.status == Status.NON_FINANCIAL:
        return None

    row = ExtractionResult(
        id=uuid.uuid4(),
        email_id=email.id,
        external_user_id=external_user_id,
        alias_hash=email.alias_hash,
    )
    status_value = apply_result_fields(row, result)
    if status_value == Status.EXTRACTION_FAILED.value:
        email.status = _EMAIL_STATUS[Status.EXTRACTION_FAILED]

    db.add(row)
    await db.flush()  # row.id needed for snippets + duplicate match

    # Phase 5 (flag-only policy): an exact-fingerprint match sets
    # duplicate_confidence + a DuplicateMatch but NEVER suppresses — the user
    # resolves it in the budgeting app (0% false-suppression guarantee).
    await duplicate_service.check_and_flag(db, row)

    for field_name, field in result.fields.items():
        if field.snippet:
            db.add(
                ExtractionSnippet(
                    extraction_id=row.id,
                    raw_snippet=field.snippet,
                    snippet_type=field_name,
                )
            )

    event = (
        "extraction.created"
        if status_value == Status.PENDING_REVIEW.value
        else "extraction.failed"
    )
    db.add(WebhookOutbox(event_type=event, payload_json=_extraction_event_payload(row, email)))
    return row
