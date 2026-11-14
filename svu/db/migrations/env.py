"""Alembic environment that runs migrations through the async engine."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import URL, Connection, pool

import svu.db.tables
from svu.db.engine import create_engine

config = context.config
target_metadata = svu.db.tables.metadata


def _database_url() -> URL | str:
    url: URL | str | None = config.attributes.get("url") or config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from svu.config import get_settings

    return get_settings().database_url


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True, compare_server_default=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
            await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline migrations are not supported")
asyncio.run(_run_async())
