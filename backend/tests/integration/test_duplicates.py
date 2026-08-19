"""Phase 5 (flag-only policy): same TRANSACTION via two different emails →
flagged with duplicate_confidence=1.0, NEVER suppressed. 0% false suppression:
two identical same-day purchases must both reach the user."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.models import DuplicateMatch, ExtractionResult, WebhookOutbox
from tests.integration.conftest import TEST_INTERNAL_SECRET

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
HEADERS = {"X-Internal-Secret": TEST_INTERNAL_SECRET}


def _payload(email_id: str, message_id: str) -> dict:
    return {
        "email_id": email_id,
        "alias_hash": "k3pzx9wql2mn8vta",
        "r2_key": f"emails/k3pzx9wql2mn8vta/{email_id}.eml",
        "from": "no.reply.alerts@chase.com",
        "to": "k3pzx9wql2mn8vta@fintrack.raksimoni.com",
        "subject": "Your account: A transaction was made",
        "message_id": message_id,
        "date_header": "Sun, 12 Jul 2026 18:14:00 +0000",
        "received_at": "2026-07-12T18:14:05Z",
    }


def _serve_fixtures(monkeypatch) -> None:
    serving = {
        "emails/k3pzx9wql2mn8vta/email-a.eml": (FIXTURES / "chase_alert.eml").read_bytes(),
        "emails/k3pzx9wql2mn8vta/email-b.eml": (FIXTURES / "chase_alert_b.eml").read_bytes(),
    }
    import app.api.internal as internal_mod

    async def _fake_get(r2_key: str) -> bytes:
        return serving[r2_key]

    monkeypatch.setattr(internal_mod.r2_client, "get_object", _fake_get)


async def test_same_transaction_is_flagged_never_suppressed(
    client, seeded, db_session, monkeypatch
):
    _serve_fixtures(monkeypatch)

    r1 = await client.post(
        "/internal/email-received", json=_payload("email-a", "<a@chase.com>"), headers=HEADERS
    )
    r2 = await client.post(
        "/internal/email-received", json=_payload("email-b", "<b@chase.com>"), headers=HEADERS
    )
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json().get("duplicate") is None  # email-level dedup did NOT fire

    rows = (
        (await db_session.execute(select(ExtractionResult).order_by(ExtractionResult.created_at)))
        .scalars()
        .all()
    )
    assert len(rows) == 2
    first, second = rows
    # BOTH live — flag-only, the user decides (could be two identical same-day buys)
    assert first.status == "pending_review"
    assert second.status == "pending_review"
    assert str(second.duplicate_confidence.normalize()) == "1"
    assert str(first.duplicate_confidence.normalize()) == "0"
    assert first.fingerprint == second.fingerprint

    match = (await db_session.execute(select(DuplicateMatch))).scalar_one()
    assert match.extraction_id == second.id
    assert match.candidate_id == first.id
    assert match.candidate_type == "exact_fingerprint"

    # BOTH rows produce extraction.created; the flagged one carries the badge
    created = [
        o
        for o in (await db_session.execute(select(WebhookOutbox))).scalars()
        if o.event_type == "extraction.created"
    ]
    assert len(created) == 2
    by_id = {o.payload_json["id"]: o.payload_json for o in created}
    assert by_id[str(first.id)]["duplicate_confidence"] == "0"
    assert by_id[str(second.id)]["duplicate_confidence"] == "1"


async def test_dismissed_earlier_row_does_not_flag(client, seeded, db_session, monkeypatch):
    """A dismissed earlier extraction is not evidence of duplication."""
    _serve_fixtures(monkeypatch)

    await client.post(
        "/internal/email-received", json=_payload("email-a", "<a@chase.com>"), headers=HEADERS
    )
    row = (await db_session.execute(select(ExtractionResult))).scalar_one()
    row.status = "dismissed"
    await db_session.commit()

    await client.post(
        "/internal/email-received", json=_payload("email-b", "<b@chase.com>"), headers=HEADERS
    )
    rows = (
        (await db_session.execute(select(ExtractionResult).order_by(ExtractionResult.created_at)))
        .scalars()
        .all()
    )
    assert rows[1].status == "pending_review"
    assert str(rows[1].duplicate_confidence.normalize()) == "0"  # not flagged
    assert (await db_session.execute(select(DuplicateMatch))).scalar_one_or_none() is None
