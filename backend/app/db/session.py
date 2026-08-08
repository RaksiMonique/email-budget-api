"""Async SQLAlchemy engine + session factory."""
from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings


def prepare_asyncpg_url(url: str) -> tuple[str, dict]:
    """Normalize a Postgres URL for asyncpg against managed providers
    (Neon / Supabase), returning (url, connect_args):

    - asyncpg speaks ``ssl=``, not libpq's ``sslmode=`` / ``channel_binding=``
      query params — strip them and enable TLS for any non-local host;
    - disable the prepared-statement cache so a pooled (PgBouncer
      transaction-mode) endpoint works; harmless on direct connections.
    """
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    sslmode = query.pop("sslmode", None)
    query.pop("channel_binding", None)

    connect_args: dict = {"statement_cache_size": 0}
    host = parts.hostname or ""
    is_local = host in ("localhost", "127.0.0.1", "")
    if sslmode not in (None, "disable") or not is_local:
        connect_args["ssl"] = True

    clean = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )
    return clean, connect_args


_url, _connect_args = prepare_asyncpg_url(get_settings().database_url)
engine = create_async_engine(_url, pool_pre_ping=True, connect_args=_connect_args)
async_session = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with async_session() as session:
        yield session
