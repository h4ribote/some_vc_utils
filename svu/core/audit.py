"""The audit log of configuration changes and administrative actions."""

from __future__ import annotations

from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from svu.db.tables import audit_log


async def record(conn: AsyncConnection, action: str, *, actor_id: int | None, guild_id: int | None = None, **detail: Any) -> None:
    await conn.execute(insert(audit_log).values(action=action, actor_id=actor_id, guild_id=guild_id, detail=detail))
