"""Phase 7: outbox delivery — encrypted config, HMAC signing, backoff, failure."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.models import WebhookConfig, WebhookOutbox
from app.services import webhook_delivery_service as delivery
from tests.integration.conftest import TEST_API_KEY

KEY = {"X-API-Key": TEST_API_KEY}
SECRET = "shared-webhook-secret-123"


async def _configure(client) -> None:
    r = await client.post(
        "/api/v1/config/webhook",
        json={"webhook_url": "http://budgeting.test/hooks/email", "webhook_secret": SECRET},
        headers=KEY,
    )
    assert r.status_code == 200 and r.json()["configured"] is True


def _due_row(**over) -> WebhookOutbox:
    defaults = dict(
        event_type="extraction.created",
        payload_json={"extraction_id": "x", "amount": "45.99"},
        status="pending",
        attempts=0,
        next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    defaults.update(over)
    return WebhookOutbox(**defaults)


async def test_secret_is_encrypted_at_rest(client, seeded, db_session):
    await _configure(client)
    cfg = (await db_session.execute(select(WebhookConfig))).scalar_one()
    assert SECRET not in cfg.webhook_secret_encrypted
    from app.security import crypto

    assert crypto.decrypt(cfg.webhook_secret_encrypted) == SECRET


async def test_delivery_signs_correctly(client, seeded, db_session):
    await _configure(client)
    db_session.add(_due_row())
    await db_session.commit()

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["ts"] = request.headers["X-EmailBudget-Timestamp"]
        captured["sig"] = request.headers["X-EmailBudget-Signature"]
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
        n = await delivery.process_due(db_session, mock_client)
    assert n == 1

    # receiver-side verification — exactly what the budgeting app will do
    expected = hmac.new(
        SECRET.encode(), f"{captured['ts']}.".encode() + captured["body"], hashlib.sha256
    ).hexdigest()
    assert hmac.compare_digest(expected, captured["sig"])

    envelope = json.loads(captured["body"])
    assert envelope["event"] == "extraction.created"
    assert envelope["data"]["amount"] == "45.99"

    row = (await db_session.execute(select(WebhookOutbox))).scalar_one()
    assert row.status == "delivered"
    assert row.delivered_at is not None
    assert row.attempts == 1


async def test_failure_backs_off_then_fails_terminally(client, seeded, db_session):
    await _configure(client)
    db_session.add(_due_row())
    await db_session.commit()

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(failing)) as mock_client:
        for attempt in range(1, delivery.MAX_ATTEMPTS + 1):
            row = (await db_session.execute(select(WebhookOutbox))).scalar_one()
            row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db_session.commit()
            await delivery.process_due(db_session, mock_client)
            row = (await db_session.execute(select(WebhookOutbox))).scalar_one()
            assert row.attempts == attempt
            if attempt < delivery.MAX_ATTEMPTS:
                assert row.status == "pending"
                assert row.next_attempt_at > datetime.now(timezone.utc)  # backed off
            else:
                assert row.status == "failed"  # surfaced, never silently lost
                assert "503" in row.last_error


async def test_no_config_defers_without_burning_attempts(client, seeded, db_session):
    db_session.add(_due_row())
    await db_session.commit()

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as c:
        await delivery.process_due(db_session, c)

    row = (await db_session.execute(select(WebhookOutbox))).scalar_one()
    assert row.status == "pending"
    assert row.attempts == 0  # not burned
    assert row.next_attempt_at > datetime.now(timezone.utc)  # deferred


async def test_webhook_test_endpoint(client, seeded, db_session, monkeypatch):
    await _configure(client)
    r = await client.post("/api/v1/config/webhook/test", headers=KEY)
    # no real receiver at budgeting.test — endpoint reports failure gracefully
    assert r.status_code == 200
    assert r.json()["delivered"] is False


# ── regression tests for review findings (2026-08-05) ────────────────────────


async def test_config_endpoint_rejects_malformed_url(client, seeded):
    """`http://[::1` passes ^https?:// but explodes as httpx.InvalidURL later."""
    r = await client.post(
        "/api/v1/config/webhook",
        json={"webhook_url": "http://[::1", "webhook_secret": SECRET},
        headers=KEY,
    )
    assert r.status_code == 422


async def test_invalid_url_fails_rows_not_batch(client, seeded, db_session):
    """A malformed URL in legacy config must fail rows individually with
    backoff, never abort the whole batch with an unhandled exception."""
    from app.models import WebhookConfig
    from app.security import crypto

    db_session.add(
        WebhookConfig(webhook_url="http://[::1", webhook_secret_encrypted=crypto.encrypt(SECRET))
    )
    db_session.add(_due_row())
    db_session.add(_due_row(event_type="extraction.failed"))
    await db_session.commit()

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as c:
        n = await delivery.process_due(db_session, c)  # must not raise
    assert n == 2

    rows = (await db_session.execute(select(WebhookOutbox))).scalars().all()
    for row in rows:
        assert row.status == "pending"
        assert row.attempts == 1  # each row failed individually with backoff
        assert "InvalidURL" in row.last_error


async def test_undecryptable_secret_defers_without_burning_attempts(client, seeded, db_session):
    """After SECRET_ENCRYPTION_KEY rotation the stored secret is unreadable —
    delivery must defer (like no-config), not stall the outbox in an error loop."""
    from app.models import WebhookConfig

    db_session.add(
        WebhookConfig(webhook_url="http://budgeting.test/hook", webhook_secret_encrypted="garbage")
    )
    db_session.add(_due_row())
    await db_session.commit()

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as c:
        await delivery.process_due(db_session, c)  # must not raise

    row = (await db_session.execute(select(WebhookOutbox))).scalar_one()
    assert row.status == "pending"
    assert row.attempts == 0  # deferred, not burned
    assert row.next_attempt_at > datetime.now(timezone.utc)
