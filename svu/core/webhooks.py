"""The webhook inbox: the API stores each verified delivery and answers at once, and the worker applies it."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from svu.core import grants, operations
from svu.db.tables import vc_app, webhook_events
from svu.vc.models import Claim
from svu.vc.webhook import EVENT_CLAIM_UPDATE, EVENT_GRANT_DECISION

log = logging.getLogger("svu.webhooks")

# The claim metadata key that names the operation a claim pays for.
OPERATION_METADATA_KEY = "svu_operation"


@dataclass(frozen=True)
class WebhookEvent:
    id: int
    type: int
    body: dict[str, Any]
    attempts: int


async def record(conn: AsyncConnection, event_type: int, body: dict[str, Any]) -> int:
    return int(await conn.scalar(insert(webhook_events).values(type=event_type, body=body).returning(webhook_events.c.id)))


async def take_next(conn: AsyncConnection) -> WebhookEvent | None:
    row = (await conn.execute(select(webhook_events).where(webhook_events.c.processed_at.is_(None)).order_by(webhook_events.c.id).limit(1).with_for_update(skip_locked=True))).first()
    return WebhookEvent(id=row.id, type=row.type, body=row.body, attempts=row.attempts) if row else None


async def mark_processed(conn: AsyncConnection, event_id: int, error: str | None = None) -> None:
    await conn.execute(update(webhook_events).where(webhook_events.c.id == event_id).values(processed_at=func.now(), error=error, attempts=webhook_events.c.attempts + 1))


async def mark_attempt_failed(conn: AsyncConnection, event_id: int, error: str) -> None:
    await conn.execute(update(webhook_events).where(webhook_events.c.id == event_id).values(error=error, attempts=webhook_events.c.attempts + 1))


def operation_hint(claim: Claim) -> uuid.UUID | None:
    value = claim.metadata.get(OPERATION_METADATA_KEY)
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def apply(conn: AsyncConnection, event: WebhookEvent) -> None:
    """Apply one delivery; types this application does not handle are ignored, as the webhook protocol requires."""
    data = event.body.get("data")
    if event.type == EVENT_CLAIM_UPDATE and isinstance(data, list):
        for item in data:
            claim = Claim.from_json(item)
            if not await operations.apply_claim(conn, claim.id, claim.status, operation_hint=operation_hint(claim)):
                log.info("claim update matched no open operation", extra={"claim_id": claim.id, "status": claim.status})
    elif event.type == EVENT_GRANT_DECISION and isinstance(data, dict):
        guild = data.get("guild_id")
        if not isinstance(guild, str) or not guild.isdigit():
            return
        guild_id = int(guild)
        scopes = data.get("scopes") or []
        grant_id = data.get("grant_id")
        if "vc.issue" in scopes:
            await grants.poll_soon(conn, guild_id)
        else:
            await grants.revoke(conn, guild_id, grant_id=str(grant_id) if grant_id is not None else None)


async def public_key(conn: AsyncConnection) -> str | None:
    return await conn.scalar(select(vc_app.c.public_key).where(vc_app.c.id == 1))


@dataclass(frozen=True)
class AppState:
    public_key: str
    webhook_url: str | None
    webhook_verified_at: datetime | None
    webhook_failed_at: datetime | None
    synced_at: datetime


async def app_state(conn: AsyncConnection) -> AppState | None:
    row = (await conn.execute(select(vc_app).where(vc_app.c.id == 1))).first()
    if row is None:
        return None
    return AppState(public_key=row.public_key, webhook_url=row.webhook_url, webhook_verified_at=row.webhook_verified_at, webhook_failed_at=row.webhook_failed_at, synced_at=row.synced_at)


async def save_app_state(conn: AsyncConnection, *, public_key: str, webhook_url: str | None, webhook_verified_at: datetime | None, webhook_failed_at: datetime | None) -> None:
    values = {"public_key": public_key, "webhook_url": webhook_url, "webhook_verified_at": webhook_verified_at, "webhook_failed_at": webhook_failed_at, "synced_at": func.now()}
    stmt = pg_insert(vc_app).values(id=1, **values)
    await conn.execute(stmt.on_conflict_do_update(index_elements=["id"], set_=values))


async def prune(conn: AsyncConnection, before: datetime) -> int:
    result = await conn.execute(webhook_events.delete().where(and_(webhook_events.c.processed_at.is_not(None), webhook_events.c.processed_at < before)))
    return int(result.rowcount)
