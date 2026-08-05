"""Phase 6: extraction results API lifecycle + feedback + stats."""
from __future__ import annotations

from sqlalchemy import select

from app.models import MerchantRule
from tests.integration.conftest import TEST_API_KEY, TEST_INTERNAL_SECRET

KEY = {"X-API-Key": TEST_API_KEY}
INTERNAL = {"X-Internal-Secret": TEST_INTERNAL_SECRET}

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


async def _ingest(client) -> str:
    r = await client.post("/internal/email-received", json=PAYLOAD, headers=INTERNAL)
    assert r.status_code == 200, r.text
    return r.json()["extraction_id"]


async def test_list_detail_preview(client, seeded, mock_r2):
    ex_id = await _ingest(client)

    page = (await client.get("/api/v1/extractions?external_user_id=user-42", headers=KEY)).json()
    assert page["total"] == 1
    item = page["items"][0]
    assert item["amount"] == "45.99"  # string, never a JSON float
    assert isinstance(item["amount"], str)
    assert item["status"] == "pending_review"

    detail = (await client.get(f"/api/v1/extractions/{ex_id}", headers=KEY)).json()
    assert detail["merchant_raw"] == "AMAZON MKTP US"
    assert detail["card_last4"] == "1234"
    assert detail["field_confidences"]["amount"] == 0.97

    preview = (await client.get(f"/api/v1/extractions/{ex_id}/preview", headers=KEY)).json()
    types = {s["snippet_type"] for s in preview["snippets"]}
    assert "amount" in types and "merchant" in types

    # status filter
    page = (
        await client.get(
            "/api/v1/extractions?external_user_id=user-42&status=confirmed", headers=KEY
        )
    ).json()
    assert page["total"] == 0


async def test_confirm_dismiss_lifecycle(client, seeded, mock_r2):
    ex_id = await _ingest(client)

    r = await client.post(
        f"/api/v1/extractions/{ex_id}/confirm", json={"category": "Shopping"}, headers=KEY
    )
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"
    assert r.json()["category_confirmed"] == "Shopping"

    # idempotent repeat
    r2 = await client.post(f"/api/v1/extractions/{ex_id}/confirm", json={}, headers=KEY)
    assert r2.status_code == 200 and r2.json()["status"] == "confirmed"
    assert r2.json()["category_confirmed"] == "Shopping"  # not clobbered by retry

    # dismiss after confirm → conflict
    r3 = await client.post(f"/api/v1/extractions/{ex_id}/dismiss", json={}, headers=KEY)
    assert r3.status_code == 409

    # reprocess after confirm → conflict (user decisions are protected)
    r4 = await client.post(f"/api/v1/extractions/{ex_id}/reprocess", headers=KEY)
    assert r4.status_code == 409


async def test_reprocess_updates_row(client, seeded, mock_r2):
    ex_id = await _ingest(client)
    r = await client.post(f"/api/v1/extractions/{ex_id}/reprocess", headers=KEY)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == ex_id  # same row, updated in place
    assert body["amount"] == "45.99"
    assert body["status"] == "pending_review"


async def test_reprocess_refreshes_duplicate_artifacts(client, seeded, mock_r2, db_session):
    """Review finding: reprocess must re-run duplicate flagging and never
    accumulate stale DuplicateMatch rows."""
    from sqlalchemy import select as sa_select

    from app.models import DuplicateMatch

    first_id = await _ingest(client)
    # second email, same transaction (different message/r2 key) → flagged
    payload_b = dict(
        PAYLOAD, message_id="<other@chase.com>", r2_key="emails/k3pzx9wql2mn8vta/b.eml"
    )
    rb = await client.post("/internal/email-received", json=payload_b, headers=INTERNAL)
    flagged_id = rb.json()["extraction_id"]

    r = await client.post(f"/api/v1/extractions/{flagged_id}/reprocess", headers=KEY)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending_review"
    assert body["duplicate_confidence"] == "1"  # re-flagged against the same twin

    matches = (
        (
            await db_session.execute(
                sa_select(DuplicateMatch).where(
                    DuplicateMatch.extraction_id.in_([flagged_id])
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(matches) == 1  # refreshed, not accumulated
    assert str(matches[0].candidate_id) == first_id


async def test_feedback_creates_rule_after_three(client, seeded, mock_r2, db_session):
    for i in range(3):
        r = await client.post(
            "/api/v1/feedback/category",
            json={"merchant_normalized": "Blue Bottle Coffee", "category_confirmed": "Food & Drink"},
            headers=KEY,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["confirmations"] == i + 1
    assert body["rule_created"] is True

    rule = (await db_session.execute(select(MerchantRule))).scalar_one()
    assert rule.pattern == "BLUE BOTTLE COFFEE"
    assert rule.match_type == "exact"
    assert rule.category == "Food & Drink"
    assert rule.confirmation_count == 3


async def test_stats(client, seeded, mock_r2):
    await _ingest(client)
    stats = (
        await client.get("/api/v1/stats/extraction?external_user_id=user-42", headers=KEY)
    ).json()
    assert stats["total_extractions"] == 1
    assert stats["by_status"] == {"pending_review": 1}
    assert stats["success_rate"] == 1.0
    assert stats["top_senders"][0]["sender"] == "chase.com"
