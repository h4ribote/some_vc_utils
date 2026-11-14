from __future__ import annotations

from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

from svu.db.migrate import head_revision
from svu.db.tables import metadata


def _diff(connection: Connection) -> list[Any]:
    context = MigrationContext.configure(connection, opts={"compare_type": True, "compare_server_default": True})
    return list(compare_metadata(context, metadata))


async def test_migrations_create_exactly_the_declared_schema(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        assert await conn.run_sync(_diff) == []


async def test_database_is_at_head(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == head_revision()


async def test_sessions_use_utc_whatever_the_server_default(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        assert await conn.scalar(text("SHOW timezone")) == "UTC"
