"""Transaction-level duplicate detection (PLAN.md Phase 5).

Policy (revised 2026-08-05): **flag, never auto-suppress.** An exact fingerprint
match (amount+currency+merchant+DAY, user-scoped) is strong evidence but NOT
proof — two identical same-day purchases (transit taps, coffees) collide at day
granularity. The success criterion is 0% false suppression, so the row keeps
status pending_review, gets duplicate_confidence=1.0 + a DuplicateMatch, and the
budgeting app shows a "possible duplicate" badge for the USER to resolve.

Auto-suppression may return in Phase 2 with stronger evidence (card_last4 +
time proximity); until then nothing is silently dropped.

Email-level dedup (same email delivered/forwarded twice) happens earlier, in
the webhook handler (r2_key + message_id). This layer catches the same
TRANSACTION arriving via different emails (bank alert + receipt, re-sent alert).
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DuplicateMatch, ExtractionResult


async def check_and_flag(db: AsyncSession, row: ExtractionResult) -> bool:
    """Flag `row` if an earlier live extraction shares its exact fingerprint.
    Sets duplicate_confidence + records a DuplicateMatch; NEVER changes status.
    Caller must have flushed `row` (needs row.id) and commits the transaction."""
    if row.fingerprint is None or row.status != "pending_review":
        return False

    earlier = (
        (
            await db.execute(
                select(ExtractionResult)
                .where(
                    ExtractionResult.fingerprint == row.fingerprint,
                    ExtractionResult.external_user_id == row.external_user_id,
                    ExtractionResult.id != row.id,
                    # flag only against live rows — a dismissed/failed earlier
                    # row is not evidence of duplication
                    ExtractionResult.status.in_(("pending_review", "confirmed")),
                )
                .order_by(ExtractionResult.created_at)
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if earlier is None:
        return False

    row.duplicate_confidence = Decimal("1")
    db.add(
        DuplicateMatch(
            extraction_id=row.id,
            candidate_id=earlier.id,
            candidate_type="exact_fingerprint",
            duplicate_confidence=Decimal("1"),
        )
    )
    return True
