"""Privacy and data deletion (PLAN.md Phase 8).

Two flows share the purge machinery:
- user-requested deletion: DELETE /users/{id}/data deactivates the user's
  aliases and stamps pending_deletion_at = now + grace on their emails;
- retention: emails older than retention_days are swept regardless.

The maintenance loop deletes the R2 object then nulls r2_key. Extraction ROWS
survive in MVP (raw email content is the sensitive artifact); full GDPR
row-deletion + export ships in Phase 2. The R2 bucket's own 90-day lifecycle
rule backstops all of this at the storage layer.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.integrations import r2_client
from app.models import Alias, ImportAuditLog, ImportedEmail

logger = logging.getLogger(__name__)

PURGE_BATCH = 100


async def schedule_user_deletion(db: AsyncSession, external_user_id: str) -> dict:
    """Deactivate all the user's aliases; schedule their raw emails for
    deletion after the grace period. Idempotent. Caller commits via this
    function (single transaction)."""
    aliases = (
        (await db.execute(select(Alias).where(Alias.external_user_id == external_user_id)))
        .scalars()
        .all()
    )
    alias_hashes = [a.alias_hash for a in aliases]
    deactivated = 0
    for alias in aliases:
        if alias.is_active:
            alias.is_active = False
            deactivated += 1

    scheduled = 0
    if alias_hashes:
        deadline = datetime.now(timezone.utc) + timedelta(
            days=get_settings().deletion_grace_days
        )
        result = await db.execute(
            update(ImportedEmail)
            .where(
                ImportedEmail.alias_hash.in_(alias_hashes),
                ImportedEmail.r2_key.is_not(None),
                ImportedEmail.pending_deletion_at.is_(None),  # idempotent
            )
            .values(pending_deletion_at=deadline)
        )
        scheduled = result.rowcount or 0

    # counts go to logs, NOT the String(32) status column — a large user's
    # counts would overflow it, 500ing + rolling back the entire deletion
    logger.info(
        "user_data_deletion_requested user=%s aliases=%s emails=%s",
        external_user_id, deactivated, scheduled,
    )
    db.add(
        ImportAuditLog(
            action="user_data_deletion_requested",
            previous_status=None,
            new_status="scheduled",
        )
    )
    await db.commit()
    return {
        "external_user_id": external_user_id,
        "aliases_deactivated": deactivated,
        "emails_scheduled_for_deletion": scheduled,
    }


MAX_DRAIN_BATCHES = 50  # up to 5,000 purges per sweep — drains large backlogs


async def _purge_batch(db: AsyncSession, rows: list[ImportedEmail]) -> int:
    """Delete R2 objects and null pointers. A missing object still nulls the
    pointer (the bucket lifecycle may have beaten us to it).

    Deliberately NO row locks held during the R2 network I/O — the UPDATE at
    the end is idempotent (re-deleting an object → R2ObjectMissing → pointer
    still nulled), so a concurrent sweep doing the same work is harmless."""
    purged = 0
    for email in rows:
        try:
            await r2_client.delete_object(email.r2_key)
        except r2_client.R2ObjectMissing:
            pass  # already gone — pointer hygiene still applies
        except Exception as exc:
            # transient R2 failure: leave the row for the next sweep
            logger.warning("purge failed for %s: %s", email.r2_key, exc)
            continue
        db.add(
            ImportAuditLog(
                email_id=email.id,
                action="raw_email_purged",
                previous_status=email.status,
                new_status=email.status,
            )
        )
        email.r2_key = None
        purged += 1
    await db.commit()
    return purged


async def _drain(db: AsyncSession, base_query) -> int:
    """Run purge batches until the queue is drained (or the safety cap hits).
    Random order prevents a block of permanently-failing rows from occupying
    the same batch slots every sweep and starving the rest."""
    from sqlalchemy import func as sa_func

    total = 0
    for _ in range(MAX_DRAIN_BATCHES):
        rows = (
            (await db.execute(base_query.order_by(sa_func.random()).limit(PURGE_BATCH)))
            .scalars()
            .all()
        )
        if not rows:
            break
        purged = await _purge_batch(db, rows)
        total += purged
        if purged == 0:
            break  # everything in this batch failed transiently — stop, retry next sweep
    return total


async def purge_due(db: AsyncSession) -> int:
    """Purge emails whose user-requested deletion grace period has passed."""
    now = datetime.now(timezone.utc)
    return await _drain(
        db,
        select(ImportedEmail).where(
            ImportedEmail.pending_deletion_at.is_not(None),
            ImportedEmail.pending_deletion_at <= now,
            ImportedEmail.r2_key.is_not(None),
        ),
    )


async def retention_sweep(db: AsyncSession) -> int:
    """Purge raw emails older than retention_days (app-level suspenders on top
    of the bucket's 90-day lifecycle belt)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=get_settings().retention_days)
    return await _drain(
        db,
        select(ImportedEmail).where(
            ImportedEmail.received_at.is_not(None),
            ImportedEmail.received_at < cutoff,
            ImportedEmail.r2_key.is_not(None),
        ),
    )
