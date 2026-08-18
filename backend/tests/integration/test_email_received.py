"""End-to-end integration: webhook payload → pipeline → rows → outbox."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from app.models import Alias, ExtractionResult, ImportedEmail, WebhookOutbox
from tests.integration.conftest import TEST_INTERNAL_SECRET

PAYLOAD = {
    "email_id": "1fc9846f-7c83-492d-8ec1-8afdc986d54a",
    "alias_hash": "k3pzx9wql2mn8vta",
    "r2_key": "emails/k3pzx9wql2mn8vta/1fc9846f.eml",
    "from": "no.reply.alerts@chase.com",
    "to": "k3pzx9wql2mn8vta@fintrack.raksimoni.com",
    "subject": "Your account: A transaction was made",
    "message_id": "<alert-20260712-abc@chase.com>",
    "date_header": "Sun, 12 Jul 2026 18:14:00 +0000",
    "received_at": "2026-07-12T18:14:05Z",
}
HEADERS = {"X-Internal-Secret": TEST_INTERNAL_SECRET}


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200


async def test_internal_secret_required(client, seeded):
    r = await client.post(
        "/internal/email-received", json=PAYLOAD, headers={"X-Internal-Secret": "wrong"}
    )
    assert r.status_code == 403


async def test_full_flow_creates_extraction_and_outbox(client, seeded, mock_r2, db_session):
    r = await client.post("/internal/email-received", json=PAYLOAD, headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["received"] is True
    assert body["status"] == "processed"
    assert body["extraction_id"] is not None

    email = (await db_session.execute(select(ImportedEmail))).scalar_one()
    assert email.status == "processed"
    assert email.resolved_sender_domain == "chase.com"
    assert email.sender_source == "dkim"

    row = (await db_session.execute(select(ExtractionResult))).scalar_one()
    assert row.amount == Decimal("45.9900")
    assert row.currency == "USD"
    assert row.merchant_normalized == "Amazon"
    assert row.status == "pending_review"
    assert row.confidence_band == "high"
    assert row.fingerprint is not None
    assert row.external_user_id == "user-42"

    events = {
        o.event_type
        for o in (await db_session.execute(select(WebhookOutbox))).scalars()
    }
    assert events == {"extraction.created", "alias.first_email_received"}

    alias = (await db_session.execute(select(Alias))).scalar_one()
    assert alias.emails_received == 1


async def test_duplicate_message_id_is_idempotent(client, seeded, mock_r2, db_session):
    r1 = await client.post("/internal/email-received", json=PAYLOAD, headers=HEADERS)
    assert r1.status_code == 200
    r2 = await client.post("/internal/email-received", json=PAYLOAD, headers=HEADERS)
    assert r2.status_code == 200
    assert r2.json().get("duplicate") is True

    emails = (await db_session.execute(select(ImportedEmail))).scalars().all()
    assert len(emails) == 1
    alias = (await db_session.execute(select(Alias))).scalar_one()
    assert alias.emails_received == 1  # duplicate did not double-count


async def test_unknown_alias_is_acked_and_dropped(client, seeded, mock_r2, db_session):
    payload = dict(PAYLOAD, alias_hash="nonexistenttoken", message_id="<other@x>")
    r = await client.post("/internal/email-received", json=payload, headers=HEADERS)
    assert r.status_code == 200  # ack — never retry unknown aliases
    assert r.json()["dropped"] == "unknown_or_inactive_alias"
    assert (await db_session.execute(select(ImportedEmail))).scalar_one_or_none() is None


async def test_alias_edge_check(client, seeded):
    ok = await client.get("/internal/aliases/k3pzx9wql2mn8vta", headers=HEADERS)
    assert ok.status_code == 200 and ok.json()["active"] is True
    missing = await client.get("/internal/aliases/unknowntoken99", headers=HEADERS)
    assert missing.status_code == 404


# ── regression tests for review findings ─────────────────────────────────────


async def test_retry_without_message_id_is_idempotent(client, seeded, mock_r2, db_session):
    """Review finding (high): emails without a Message-ID must dedup on r2_key."""
    payload = dict(PAYLOAD, message_id="")
    r1 = await client.post("/internal/email-received", json=payload, headers=HEADERS)
    assert r1.status_code == 200
    r2 = await client.post("/internal/email-received", json=payload, headers=HEADERS)
    assert r2.status_code == 200
    assert r2.json().get("duplicate") is True

    from app.models import Alias, ImportedEmail

    emails = (await db_session.execute(select(ImportedEmail))).scalars().all()
    assert len(emails) == 1
    alias = (await db_session.execute(select(Alias))).scalar_one()
    assert alias.emails_received == 1


async def test_huge_amount_degrades_not_dataerror(client, seeded, db_session, monkeypatch):
    """Review finding (high): out-of-range amount → extraction_failed, never 500."""
    evil = (
        b"From: someone@example.com\r\n"
        b"To: k3pzx9wql2mn8vta@fintrack.raksimoni.com\r\n"
        b"Subject: your receipt\r\n"
        b"Message-ID: <evil-huge@x>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"Total charged USD 999999999999.99 thanks!\r\n"
    )

    import app.api.internal as internal_mod

    async def _fake_get(r2_key: str) -> bytes:
        return evil

    monkeypatch.setattr(internal_mod.r2_client, "get_object", _fake_get)

    payload = dict(PAYLOAD, message_id="<evil-huge@x>", r2_key="emails/k/evil.eml")
    r = await client.post("/internal/email-received", json=payload, headers=HEADERS)
    assert r.status_code == 200, r.text  # 500 here = poison-message regression

    row = (await db_session.execute(select(ExtractionResult))).scalar_one()
    assert row.amount is None
    assert row.status == "extraction_failed"
    events = {
        o.event_type
        for o in (await db_session.execute(select(WebhookOutbox))).scalars()
    }
    assert "extraction.failed" in events and "extraction.created" not in events


async def test_overlong_message_id_is_clamped(client, seeded, mock_r2, db_session):
    """Review finding (medium): 1200-char Message-ID must not DataError→DLQ."""
    long_id = "<" + "x" * 1200 + "@broken-mta>"
    payload = dict(PAYLOAD, message_id=long_id, r2_key="emails/k/longid.eml")
    r = await client.post("/internal/email-received", json=payload, headers=HEADERS)
    assert r.status_code == 200, r.text

    email = (await db_session.execute(select(ImportedEmail))).scalar_one()
    assert email.message_id is not None and len(email.message_id) <= 998


async def test_rate_limit_drops_flood_over_budget(
    client, seeded, mock_r2, db_session, monkeypatch
):
    """Leaked-alias flood protection: with a per-alias cap set, emails beyond the
    budget in the window are ACKed + dropped (rate_limited) and never persisted —
    so a flood can't clutter pending_review. Under-cap emails still process."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "rate_limit_per_alias", 2)

    accepted = dropped = 0
    for i in range(4):
        payload = dict(
            PAYLOAD,
            message_id=f"<flood-{i}@x>",
            r2_key=f"emails/k3pzx9wql2mn8vta/flood-{i}.eml",
        )
        r = await client.post("/internal/email-received", json=payload, headers=HEADERS)
        assert r.status_code == 200, r.text  # always ACK — never retry a flood
        if r.json().get("dropped") == "rate_limited":
            dropped += 1
        else:
            accepted += 1

    assert accepted == 2 and dropped == 2  # exactly the budget accepted

    emails = (await db_session.execute(select(ImportedEmail))).scalars().all()
    assert len(emails) == 2  # dropped ones left no rows
    alias = (await db_session.execute(select(Alias))).scalar_one()
    assert alias.emails_received == 2


async def test_refund_sets_credit_direction_end_to_end(client, seeded, db_session, monkeypatch):
    """A refund must extract as direction=credit (is_probable_refund) so the ledger
    reduces — not inflates — spending, and the webhook payload must carry it."""
    refund = (
        b"From: alerts@somebank.test\r\n"
        b"To: k3pzx9wql2mn8vta@fintrack.raksimoni.com\r\n"
        b"Subject: Refund processed\r\n"
        b"Message-ID: <refund-1@somebank.test>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"Transaction Type: Refund\r\n"
        b"Amount: JMD 500.00\r\n"
        b"Merchant: SAMPLE STORE\r\n"
        b"Transaction Date: 06/08/2026\r\n"
    )

    import app.api.internal as internal_mod

    async def _fake_get(r2_key: str) -> bytes:
        return refund

    monkeypatch.setattr(internal_mod.r2_client, "get_object", _fake_get)

    payload = dict(PAYLOAD, message_id="<refund-1@somebank.test>", r2_key="emails/k/refund.eml")
    r = await client.post("/internal/email-received", json=payload, headers=HEADERS)
    assert r.status_code == 200, r.text

    row = (await db_session.execute(select(ExtractionResult))).scalar_one()
    assert row.direction == "credit"
    assert row.is_probable_refund is True
    assert row.is_declined is False
    assert row.status == "pending_review"

    outbox = (
        await db_session.execute(
            select(WebhookOutbox).where(WebhookOutbox.event_type == "extraction.created")
        )
    ).scalar_one()
    assert outbox.payload_json["direction"] == "credit"
    assert outbox.payload_json["is_probable_refund"] is True


async def test_rate_limit_disabled_by_default(client, seeded, mock_r2, db_session):
    """Default (limit=0) is off: a burst all processes — no MVP behavior change."""
    for i in range(3):
        payload = dict(
            PAYLOAD, message_id=f"<burst-{i}@x>", r2_key=f"emails/k/burst-{i}.eml"
        )
        r = await client.post("/internal/email-received", json=payload, headers=HEADERS)
        assert r.status_code == 200 and "dropped" not in r.json()

    emails = (await db_session.execute(select(ImportedEmail))).scalars().all()
    assert len(emails) == 3
