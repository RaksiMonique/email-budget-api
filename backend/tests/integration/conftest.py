"""Integration test fixtures: real PostgreSQL (docker), mocked R2."""
from __future__ import annotations

import os
from pathlib import Path

# Must be set BEFORE any app import — get_settings() is cached.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/email_budget_test",
)
os.environ.setdefault("INTERNAL_SECRET", "test-internal-secret")
os.environ.setdefault("SECRET_ENCRYPTION_KEY", "test-encryption-key-material")
os.environ.setdefault("ENABLE_OUTBOX_POLLER", "false")

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings

get_settings.cache_clear()

from app.db.base import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Alias, ApiKey  # noqa: E402,F401
from app.security.api_key import hash_key  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"

TEST_API_KEY = "eb_test_key_not_secret"
TEST_INTERNAL_SECRET = "test-internal-secret"


@pytest.fixture()
async def engine():
    # create the test database if missing (connect via the default 'postgres' db)
    admin = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5432/postgres",
        isolation_level="AUTOCOMMIT",
    )
    from sqlalchemy import text

    async with admin.connect() as conn:
        exists = await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname='email_budget_test'")
        )
        if not exists:
            await conn.execute(text("CREATE DATABASE email_budget_test"))
    await admin.dispose()

    eng = create_async_engine(get_settings().database_url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture()
async def db_session(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session


@pytest.fixture()
async def client(engine):
    app = create_app()
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_db():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture()
async def seeded(db_session):
    """A known alias + API key."""
    db_session.add(Alias(alias_hash="k3pzx9wql2mn8vta", external_user_id="user-42"))
    db_session.add(ApiKey(key_hash=hash_key(TEST_API_KEY), label="test"))
    await db_session.commit()


@pytest.fixture()
def mock_r2(monkeypatch):
    """Serve the synthetic Chase alert for any r2_key."""
    raw = (FIXTURES / "chase_alert.eml").read_bytes()

    async def _fake_get(r2_key: str) -> bytes:
        return raw

    import app.api.internal as internal_mod

    monkeypatch.setattr(internal_mod.r2_client, "get_object", _fake_get)
    return raw
