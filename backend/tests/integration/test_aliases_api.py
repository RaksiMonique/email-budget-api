"""Alias CRUD via the budgeting-app-facing API (X-API-Key auth)."""
from __future__ import annotations

from tests.integration.conftest import TEST_API_KEY

KEY = {"X-API-Key": TEST_API_KEY}


async def test_requires_api_key(client, seeded):
    r = await client.post("/api/v1/aliases", json={"external_user_id": "user-7"})
    assert r.status_code == 401


async def test_alias_lifecycle(client, seeded):
    # create
    r = await client.post(
        "/api/v1/aliases", json={"external_user_id": "user-7", "label": "main"}, headers=KEY
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["email_address"].endswith("@fintrack.raksimoni.com")
    assert created["alias_hash"] == created["alias_hash"].lower()
    assert len(created["alias_hash"]) >= 8
    assert created["emails_received"] == 0

    # list
    r = await client.get("/api/v1/aliases?external_user_id=user-7", headers=KEY)
    assert [a["id"] for a in r.json()] == [created["id"]]

    # detail (the onboarding poll target)
    r = await client.get(f"/api/v1/aliases/{created['id']}", headers=KEY)
    assert r.status_code == 200
    assert r.json()["emails_received"] == 0

    # deactivate
    r = await client.delete(f"/api/v1/aliases/{created['id']}", headers=KEY)
    assert r.status_code == 204
    r = await client.get(f"/api/v1/aliases/{created['id']}", headers=KEY)
    assert r.json()["is_active"] is False
