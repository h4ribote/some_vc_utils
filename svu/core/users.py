"""Per-user preferences."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from svu.db.tables import users


async def receives_dm(conn: AsyncConnection, user_id: int) -> bool:
    return bool(await conn.scalar(select(users.c.receive_dm).where(users.c.discord_id == user_id)))


async def set_receive_dm(conn: AsyncConnection, user_id: int, enabled: bool) -> None:
    stmt = pg_insert(users).values(discord_id=user_id, receive_dm=enabled)
    await conn.execute(stmt.on_conflict_do_update(index_elements=["discord_id"], set_={"receive_dm": enabled, "updated_at": func.now()}))
