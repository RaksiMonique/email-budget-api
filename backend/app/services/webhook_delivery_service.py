"""Outbox-based webhook delivery (PLAN.md Phase 7).

Events are written to webhook_outbox transactionally with the work they
announce; this service delivers them with backoff. Survives redeploys by
construction — state lives in Postgres, not in sleeping coroutines.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WebhookConfig, WebhookOutbox
from app.security import crypto

logger = logging.getLogger(__name__)

# retry schedule: immediate (on insert) → 1m → 5m → 15m → 1h, then failed
BACKOFF_SECONDS = [60, 300, 900, 3600]
MAX_ATTEMPTS = 5
BATCH = 20
NO_CONFIG_RETRY_SECONDS = 60
POLL_INTERVAL_SECONDS = 5


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """HMAC-SHA256 over '{timestamp}.{body}' — timestamp binding prevents replay."""
    msg = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def build_request(row: WebhookOutbox, secret: str) -> tuple[bytes, dict]:
    body = json.dumps(
        {
            "event": row.event_type,
            "event_id": str(row.id),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "data": row.payload_json,
        },
        separators=(",", ":"),
    ).encode()
    ts = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "X-EmailBudget-Timestamp": str(ts),
        "X-EmailBudget-Signature": sign(secret, ts, body),
    }
    return body, headers


async def get_config(db: AsyncSession, api_key_id=None) -> tuple[str, str] | None:
    """Webhook config → (url, decrypted secret), or None if unset OR undecryptable
    (e.g. after SECRET_ENCRYPTION_KEY rotation) — an unreadable config must defer
    delivery, never stall the whole outbox in an error loop.

    Per-key routing: a keyed event (api_key_id set) resolves STRICTLY to that key's
    config — never another key's, so dev and prod receivers can't cross-deliver. A
    legacy/global event (api_key_id None) resolves to the latest config of any kind,
    preserving the pre-per-key single-target behavior for already-wired setups.
    """
    if api_key_id is not None:
        stmt = (
            select(WebhookConfig)
            .where(WebhookConfig.api_key_id == api_key_id)
            .order_by(WebhookConfig.created_at.desc())
            .limit(1)
        )
    else:
        stmt = select(WebhookConfig).order_by(WebhookConfig.created_at.desc()).limit(1)
    cfg = (await db.execute(stmt)).scalars().first()
    if cfg is None:
        return None
    try:
        return cfg.webhook_url, crypto.decrypt(cfg.webhook_secret_encrypted)
    except Exception:
        logger.error(
            "webhook config secret is undecryptable (SECRET_ENCRYPTION_KEY rotated?) — "
            "re-POST /api/v1/config/webhook; deferring all outbox delivery"
        )
        return None


async def process_due(db: AsyncSession, client: httpx.AsyncClient) -> int:
    """Deliver due pending rows once. Returns number of rows processed.
    Caller-committed? No — commits itself (one transaction per batch)."""
    now = datetime.now(timezone.utc)
    rows = (
        (
            await db.execute(
                select(WebhookOutbox)
                .where(WebhookOutbox.status == "pending", WebhookOutbox.next_attempt_at <= now)
                .order_by(WebhookOutbox.next_attempt_at)
                .limit(BATCH)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0

    # resolve each row's receiver by its routing key, cached per batch so a key's
    # config is looked up once even across many events
    cfg_cache: dict = {}

    async def _cfg_for(api_key_id):
        if api_key_id not in cfg_cache:
            cfg_cache[api_key_id] = await get_config(db, api_key_id)
        return cfg_cache[api_key_id]

    for row in rows:
        cfg = await _cfg_for(row.api_key_id)
        if cfg is None:
            # no receiver configured for this key yet — wait, don't burn attempts
            row.next_attempt_at = now + timedelta(seconds=NO_CONFIG_RETRY_SECONDS)
            continue
        url, secret = cfg
        body, headers = build_request(row, secret)
        try:
            resp = await client.post(url, content=body, headers=headers)
            ok, info = resp.is_success, f"HTTP {resp.status_code}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # broad on purpose: httpx.InvalidURL is NOT an HTTPError — one
            # malformed URL must fail THIS row, not abort the whole batch
            ok, info = False, f"{type(exc).__name__}: {exc}"

        row.attempts += 1
        row.target_url = url
        if ok:
            row.status = "delivered"
            row.delivered_at = now
            row.last_error = None
        else:
            row.last_error = info[:1000]
            if row.attempts >= MAX_ATTEMPTS:
                row.status = "failed"  # surfaced for inspection, never silently lost
            else:
                row.next_attempt_at = now + timedelta(
                    seconds=BACKOFF_SECONDS[min(row.attempts - 1, len(BACKOFF_SECONDS) - 1)]
                )

    await db.commit()
    return len(rows)


async def poller_loop() -> None:
    """Background loop started from the app lifespan. State is all in Postgres,
    so crashes/redeploys lose nothing — the next loop picks up where we left off."""
    from app.db.session import async_session  # late import: test overrides

    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                async with async_session() as db:
                    await process_due(db, client)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("outbox poller iteration failed")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
