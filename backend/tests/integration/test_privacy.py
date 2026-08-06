"""Phase 8: user data deletion, grace-period purge, retention sweep."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import Alias, ImportedEmail
from app.services import privacy_service
from tests.integration.conftest import TEST_API_KEY

KEY = {"X-API-Key": TEST_API_KEY}


def _email(alias_hash: str, r2_key: str | None, **over) -> ImportedEmail:
    defaults = dict(
        alias_hash=alias_hash,
        r2_key=r2_key,
        status="processed",
        received_at=datetime.now(timezone.utc),
    )
    defaults.update(over)
    return ImportedEmail(**defaults)


async def test_delete_user_data_schedules_and_deactivates(client, seeded, db_session):
    db_session.add(_email("k3pzx9wql2mn8vta", "emails/k3pzx9wql2mn8vta/one.eml"))
    db_session.add(_email("k3pzx9wql2mn8vta", None))  # already purged — not re-scheduled
    await db_session.commit()

    r = await client.delete("/api/v1/users/user-42/data", headers=KEY)
    assert r.status_code == 200
    body = r.json()
    assert body["aliases_deactivated"] == 1
    assert body["emails_scheduled_for_deletion"] == 1

    alias = (await db_session.execute(select(Alias))).scalar_one()
    assert alias.is_active is False

    scheduled = (
        (
            await db_session.execute(
                select(ImportedEmail).where(ImportedEmail.pending_deletion_at.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    assert len(scheduled) == 1
    grace = scheduled[0].pending_deletion_at - datetime.now(timezone.utc)
    assert timedelta(days=29) < grace < timedelta(days=31)

    # idempotent: repeat schedules nothing new
    r2 = await client.delete("/api/v1/users/user-42/data", headers=KEY)
    assert r2.json()["emails_scheduled_for_deletion"] == 0
    assert r2.json()["aliases_deactivated"] == 0


async def test_purge_due_deletes_r2_and_nulls_pointer(client, seeded, db_session, monkeypatch):
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    future = datetime.now(timezone.utc) + timedelta(days=29)
    db_session.add(_email("k3pzx9wql2mn8vta", "emails/k/due.eml", pending_deletion_at=past))
    db_session.add(_email("k3pzx9wql2mn8vta", "emails/k/not-due.eml", pending_deletion_at=future))
    await db_session.commit()

    deleted_keys: list[str] = []

    async def _fake_delete(r2_key: str) -> None:
        deleted_keys.append(r2_key)

    from app.services import privacy_service as ps

    monkeypatch.setattr(ps.r2_client, "delete_object", _fake_delete)

    purged = await privacy_service.purge_due(db_session)
    assert purged == 1
    assert deleted_keys == ["emails/k/due.eml"]

    remaining = {
        e.r2_key
        for e in (await db_session.execute(select(ImportedEmail))).scalars()
    }
    # due row purged (r2_key nulled); not-due row untouched
    assert remaining == {None, "emails/k/not-due.eml"}


async def test_purge_tolerates_already_deleted_object(client, seeded, db_session, monkeypatch):
    """Bucket lifecycle may delete first — pointer hygiene must still happen."""
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.add(_email("k3pzx9wql2mn8vta", "emails/k/gone.eml", pending_deletion_at=past))
    await db_session.commit()

    from app.services import privacy_service as ps

    async def _missing(r2_key: str) -> None:
        raise ps.r2_client.R2ObjectMissing(r2_key)

    monkeypatch.setattr(ps.r2_client, "delete_object", _missing)

    assert await privacy_service.purge_due(db_session) == 1
    row = (await db_session.execute(select(ImportedEmail))).scalar_one()
    assert row.r2_key is None


async def test_retention_sweep(client, seeded, db_session, monkeypatch):
    ancient = datetime.now(timezone.utc) - timedelta(days=91)
    db_session.add(_email("k3pzx9wql2mn8vta", "emails/k/old.eml", received_at=ancient))
    db_session.add(_email("k3pzx9wql2mn8vta", "emails/k/fresh.eml"))
    await db_session.commit()

    from app.services import privacy_service as ps

    async def _fake_delete(r2_key: str) -> None:
        pass

    monkeypatch.setattr(ps.r2_client, "delete_object", _fake_delete)

    assert await privacy_service.retention_sweep(db_session) == 1
    remaining = {
        e.r2_key
        for e in (await db_session.execute(select(ImportedEmail))).scalars()
    }
    # old row purged (r2_key nulled); fresh row untouched
    assert remaining == {None, "emails/k/fresh.eml"}


async def test_export_stub_returns_501(client, seeded):
    r = await client.get("/api/v1/users/user-42/data-export", headers=KEY)
    assert r.status_code == 501
    assert "Phase 2" in r.json()["detail"]


# ── review regressions (2026-08-05): orphaned R2 objects on drop paths ───────

from tests.integration.conftest import TEST_INTERNAL_SECRET  # noqa: E402

INTERNAL = {"X-Internal-Secret": TEST_INTERNAL_SECRET}


def _webhook_payload(email_id: str, alias: str = "k3pzx9wql2mn8vta", message_id: str = "") -> dict:
    return {
        "email_id": email_id,
        "alias_hash": alias,
        "r2_key": f"emails/{alias}/{email_id}.eml",
        "from": "no.reply.alerts@chase.com",
        "to": f"{alias}@fintrack.raksimoni.com",
        "subject": "alert",
        "message_id": message_id,
        "date_header": "",
        "received_at": "2026-08-05T10:00:00Z",
    }


async def test_post_deletion_arrival_is_deleted_not_orphaned(
    client, seeded, mock_r2, db_session, monkeypatch
):
    """Email arriving after the user's deletion request must have its stored
    R2 object deleted at drop time — not orphaned until the 90-day lifecycle."""
    deleted: list[str] = []

    import app.api.internal as internal_mod

    async def _fake_delete(r2_key: str) -> None:
        deleted.append(r2_key)

    monkeypatch.setattr(internal_mod.r2_client, "delete_object", _fake_delete)

    await client.delete("/api/v1/users/user-42/data", headers=KEY)  # deactivates alias

    r = await client.post(
        "/internal/email-received", json=_webhook_payload("late-arrival"), headers=INTERNAL
    )
    assert r.status_code == 200
    assert r.json()["dropped"] == "unknown_or_inactive_alias"
    assert deleted == ["emails/k3pzx9wql2mn8vta/late-arrival.eml"]


async def test_retry_after_commit_with_deactivated_alias_keeps_owned_object(
    client, seeded, mock_r2, db_session, monkeypatch
):
    """Queue retry of already-committed work whose alias was deactivated in
    between must return duplicate — and must NOT delete the row-owned object."""
    deleted: list[str] = []

    import app.api.internal as internal_mod

    async def _fake_delete(r2_key: str) -> None:
        deleted.append(r2_key)

    payload = _webhook_payload("committed", message_id="<x@chase.com>")
    r1 = await client.post("/internal/email-received", json=payload, headers=INTERNAL)
    assert r1.status_code == 200 and r1.json().get("duplicate") is None

    monkeypatch.setattr(internal_mod.r2_client, "delete_object", _fake_delete)
    await client.delete("/api/v1/users/user-42/data", headers=KEY)

    r2 = await client.post("/internal/email-received", json=payload, headers=INTERNAL)
    assert r2.status_code == 200
    assert r2.json().get("duplicate") is True  # dedup runs BEFORE the alias check
    assert deleted == []  # the committed row still owns its object


async def test_reforwarded_duplicate_copy_is_deleted(
    client, seeded, mock_r2, db_session, monkeypatch
):
    """Same Message-ID re-forwarded (new r2_key): the second raw copy is a
    permanent orphan unless deleted at drop time."""
    deleted: list[str] = []

    import app.api.internal as internal_mod

    async def _fake_delete(r2_key: str) -> None:
        deleted.append(r2_key)

    monkeypatch.setattr(internal_mod.r2_client, "delete_object", _fake_delete)

    p1 = _webhook_payload("original", message_id="<same@chase.com>")
    p2 = _webhook_payload("reforward", message_id="<same@chase.com>")
    await client.post("/internal/email-received", json=p1, headers=INTERNAL)
    r = await client.post("/internal/email-received", json=p2, headers=INTERNAL)
    assert r.json().get("duplicate") is True
    assert deleted == ["emails/k3pzx9wql2mn8vta/reforward.eml"]