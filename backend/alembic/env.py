"""Alembic environment — async engine, metadata from app models."""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import app.models  # noqa: F401 — registers every table on Base.metadata
from app.config import get_settings
from app.db.base import Base
from app.db.session import prepare_asyncpg_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
# same normalization the app engine uses — so boot-time migrations against a
# managed provider (Neon/Supabase) get TLS + pooler-safe settings too
_db_url, _db_connect_args = prepare_asyncpg_url(get_settings().database_url)
config.set_main_option("sqlalchemy.url", _db_url)


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    connectable = create_async_engine(
        _db_url, poolclass=pool.NullPool, connect_args=_db_connect_args
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
