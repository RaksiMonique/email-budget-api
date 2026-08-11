"""Internal routes (Cloudflare Workers → FastAPI, X-Internal-Secret auth).

POST /internal/email-received processes SYNCHRONOUSLY in one transaction and
returns 200 only after commit — Cloudflare Queues retry on non-200, which is
the entire durability story (PLAN.md Phase 3). No asyncio.create_task.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_db
from app.extraction.pipeline import run as run_pipeline
from app.integrations import r2_client
from app.models import Alias, ImportedEmail, WebhookOutbox
from app.schemas.internal import EmailReceivedPayload
from app.security.internal_secret import require_internal_secret
from app.services.extraction_service import persist_result

router = APIRouter(
    prefix="/internal", dependencies=[Depends(require_internal_secret)], tags=["internal"]
)


@router.get("/aliases/{alias_hash}")
async def check_alias(alias_hash: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Edge validation for the Email Worker: 200 active, 410 deactivated, 404 unknown."""
    alias = (
        await db.execute(select(Alias).where(Alias.alias_hash == alias_hash.lower()))
    ).scalar_one_or_none()
    if alias is None:
        raise HTTPException(status_code=404, detail="unknown alias")
    if not alias.is_active:
        raise HTTPException(status_code=410, detail="alias deactivated")
    return {"active": True}


def _parse_received_at(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _delete_orphan(r2_key: str) -> None:
    """Best-effort deletion of a stored object no DB row will ever own.
    Failures are logged, not raised — the 90-day bucket lifecycle backstops."""
    import logging

    try:
        await r2_client.delete_object(r2_key)
    except Exception as exc:
        logging.getLogger(__name__).warning("orphan cleanup failed for %s: %s", r2_key, exc)


@router.post("/email-received")
async def email_received(
    payload: EmailReceivedPayload, db: AsyncSession = Depends(get_db)
) -> dict:
    alias_hash = payload.alias_hash.lower()

    # Clamp queue-payload strings to column limits — an over-length header must
    # degrade gracefully, never DataError→500→retry→DLQ (poison message).
    r2_key = payload.r2_key[:512]
    message_id = payload.message_id[:998] or None
    from_address = payload.from_address[:320] or None

    # 1. Idempotency FIRST (before the alias check): a queue retry of already-
    # committed work must return duplicate regardless of alias state — the
    # alias may have been deactivated between attempts, and this ordering is
    # what makes the orphan-deletion below safe (no row owns this r2_key).
    existing = (
        await db.execute(select(ImportedEmail.id).where(ImportedEmail.r2_key == r2_key))
    ).scalar_one_or_none()
    if existing is not None:
        return {"received": True, "duplicate": True, "email_id": str(existing)}

    # 2. Alias must exist and be active (defense in depth — the Worker's edge
    # check fails open while FastAPI is unreachable). Ack (200) so the queue
    # never retries — but DELETE the already-stored raw object first, else it
    # is orphaned in R2 (no DB row → invisible to every purge query), which
    # would break the user-deletion promise for post-deletion arrivals.
    # FOR UPDATE serializes concurrent deliveries for the same alias, making
    # the emails_received counter and the one-time first-email event race-free.
    alias = (
        await db.execute(
            select(Alias).where(Alias.alias_hash == alias_hash).with_for_update()
        )
    ).scalar_one_or_none()
    if alias is None or not alias.is_active:
        await _delete_orphan(r2_key)
        return {"received": True, "dropped": "unknown_or_inactive_alias"}

    # 3. Idempotency, secondary: alias-scoped message_id dedup catches the
    # same email *re-forwarded* (new r2_key, same Message-ID). The incoming
    # object is a second raw copy no row will ever own — delete it too.
    if message_id:
        existing = (
            await db.execute(
                select(ImportedEmail.id).where(
                    ImportedEmail.alias_hash == alias_hash,
                    ImportedEmail.message_id == message_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            await _delete_orphan(r2_key)
            return {"received": True, "duplicate": True, "email_id": str(existing)}

    # 3b. Per-alias rate limit (leaked-alias flood protection). Runs AFTER dedup
    # so a legitimate retry/re-forward never burns budget, and while holding the
    # alias FOR UPDATE lock so the count→accept decision is race-free. Over-limit
    # emails are ACKed + orphan-deleted (never 429 — a non-200 would make the
    # queue retry the flood forever). 0 = disabled.
    settings = get_settings()
    if settings.rate_limit_per_alias > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=settings.rate_limit_window_seconds
        )
        recent = await db.scalar(
            select(func.count())
            .select_from(ImportedEmail)
            .where(
                ImportedEmail.alias_hash == alias_hash,
                ImportedEmail.created_at >= cutoff,
            )
        )
        if (recent or 0) >= settings.rate_limit_per_alias:
            await _delete_orphan(r2_key)
            return {"received": True, "dropped": "rate_limited"}

    # 3. Create the email record
    email = ImportedEmail(
        alias_hash=alias_hash,
        r2_key=r2_key,
        from_address=from_address,
        subject=payload.subject or None,
        message_id=message_id,
        received_at=_parse_received_at(payload.received_at),
        status="received",
    )
    db.add(email)
    await db.flush()

    # 4. Fetch raw bytes. A missing R2 object is permanent — record and ack
    # (retrying can never succeed); transient R2 errors raise → 500 → retry.
    try:
        raw = await r2_client.get_object(r2_key)
    except r2_client.R2ObjectMissing:
        email.status = "error"
        email.processing_errors = f"R2 object missing: {r2_key}"
        await db.commit()
        return {"received": True, "error": "r2_object_missing", "email_id": str(email.id)}

    # 5. Run the pure pipeline + persist everything in this one transaction
    result = run_pipeline(raw)
    row = await persist_result(db, email, alias.external_user_id, result)

    # 6. Count every accepted email; fire the one-time first-email event
    was_first = alias.emails_received == 0
    await db.execute(
        update(Alias)
        .where(Alias.id == alias.id)
        .values(emails_received=Alias.emails_received + 1)
    )
    if was_first:
        db.add(
            WebhookOutbox(
                event_type="alias.first_email_received",
                payload_json={
                    "alias_hash": alias_hash,
                    "external_user_id": alias.external_user_id,
                    "email_id": str(email.id),
                },
            )
        )

    # 7. Commit, THEN 200. Any exception above → 500 → Cloudflare Queue retries.
    await db.commit()
    return {
        "received": True,
        "email_id": str(email.id),
        "status": email.status,
        "extraction_id": str(row.id) if row else None,
    }
