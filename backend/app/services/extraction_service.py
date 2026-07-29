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

# pipeline status -> imported_emails.status
_EMAIL_STATUS = {
    Status.PENDING_REVIEW: "processed",
    Status.EXTRACTION_FAILED: "extraction_failed",
    Status.NON_FINANCIAL: "non_financial",
    Status.FORWARDING_VERIFICATION: "forwarding_verification",
}


def _amount_str(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


# Numeric(15,4) holds < 10^11 in the integer part. An attacker-deliverable
# "USD 999999999999.99" must degrade to extraction_failed, never DataError→
# 500→retry→DLQ (poison message).
_MAX_AMOUNT = Decimal("1e11")


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

    amount = result.value("amount")
    status_value = result.status.value  # pending_review | extraction_failed
    if amount is not None and abs(amount) >= _MAX_AMOUNT:
        amount = None  # out of column range — required field now missing
        status_value = Status.EXTRACTION_FAILED.value
        email.status = _EMAIL_STATUS[Status.EXTRACTION_FAILED]

    methods = {f.method for f in result.fields.values()}
    row = ExtractionResult(
        id=uuid.uuid4(),
        email_id=email.id,
        external_user_id=external_user_id,
        alias_hash=email.alias_hash,
        amount=amount,
        currency=result.value("currency"),
        merchant_raw=result.value("merchant"),
        merchant_normalized=result.merchant_normalized,
        category_suggestion=result.category_suggestion,
        transaction_date=result.value("transaction_date"),
        card_last4=result.value("card_last4"),
        extraction_confidence=Decimal(str(result.extraction_confidence)),
        confidence_band=result.confidence_band,
        field_confidences=result.field_confidences,
        method=("mixed" if len(methods) > 1 else next(iter(methods), None)),
        status=status_value,
        fingerprint=result.fingerprint if amount is not None else None,
    )
    db.add(row)

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
    db.add(
        WebhookOutbox(
            event_type=event,
            payload_json={
                "extraction_id": str(row.id),
                "email_id": str(email.id),
                "external_user_id": external_user_id,
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
                "status": row.status,
            },
        )
    )
    return row
