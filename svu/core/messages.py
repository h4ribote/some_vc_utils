"""The Discord message outbox: rows written in the transaction that causes them and delivered by the worker afterwards."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from svu.db.tables import discord_messages

TEMPLATE_SEND_RECEIVED = "send_received"
TEMPLATE_GRANT_APPROVED = "grant_approved"
TEMPLATE_GRANT_REVOKED = "grant_revoked"
TEMPLATE_GRANT_FAILED = "grant_failed"


async def enqueue_operation_update(conn: AsyncConnection, operation_id: uuid.UUID) -> None:
    """Show the operation's state to its requester; the worker renders the state current at delivery."""
    await conn.execute(insert(discord_messages).values(kind="operation", operation_id=operation_id))


async def enqueue_dm(conn: AsyncConnection, user_id: int, template: str, **params: Any) -> None:
    await conn.execute(insert(discord_messages).values(kind="dm", user_id=user_id, payload={"template": template, **params}))
