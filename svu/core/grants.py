"""Guild issuing permission: the device-flow request an administrator approves with /grant approve, and the guild token it yields."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from svu.core import guilds
from svu.core.messages import TEMPLATE_GRANT_APPROVED, TEMPLATE_GRANT_FAILED, TEMPLATE_GRANT_REVOKED, enqueue_dm
from svu.db.tables import grant_requests, issue_grants


@dataclass(frozen=True)
class Grant:
    guild_id: int
    grant_id: str | None
    unit: str
    access_token_enc: str
    access_expires_at: datetime
    refresh_token_enc: str | None


@dataclass(frozen=True)
class GrantRequestRow:
    id: uuid.UUID
    guild_id: int
    requested_by: int
    device_code_enc: str
    user_code: str
    status: str
    expires_at: datetime


def _grant(row: object) -> Grant:
    return Grant(**{name: getattr(row, name) for name in Grant.__dataclass_fields__})


def _request(row: object) -> GrantRequestRow:
    return GrantRequestRow(**{name: getattr(row, name) for name in GrantRequestRow.__dataclass_fields__})


async def create_request(conn: AsyncConnection, guild_id: int, requested_by: int, device_code_enc: str, user_code: str, expires_at: datetime) -> uuid.UUID:
    """Record a new request for the guild; an older pending request for the same guild is superseded."""
    await conn.execute(update(grant_requests).where(and_(grant_requests.c.guild_id == guild_id, grant_requests.c.status == "pending")).values(status="expired", completed_at=func.now()))
    request_id = uuid.uuid4()
    await conn.execute(
        insert(grant_requests).values(id=request_id, guild_id=guild_id, requested_by=requested_by, device_code_enc=device_code_enc, user_code=user_code, status="pending", expires_at=expires_at)
    )
    return request_id


async def pending_request(conn: AsyncConnection, guild_id: int) -> GrantRequestRow | None:
    row = (await conn.execute(select(grant_requests).where(and_(grant_requests.c.guild_id == guild_id, grant_requests.c.status == "pending")))).first()
    return _request(row) if row else None


async def due_requests(conn: AsyncConnection, now: datetime, limit: int = 50) -> list[GrantRequestRow]:
    rows = await conn.execute(select(grant_requests).where(and_(grant_requests.c.status == "pending", grant_requests.c.next_poll_at <= now)).order_by(grant_requests.c.next_poll_at).limit(limit))
    return [_request(r) for r in rows]


async def poll_soon(conn: AsyncConnection, guild_id: int | None = None) -> None:
    """Make pending requests due now, for a type 3 webhook that says an approval happened."""
    stmt = update(grant_requests).where(grant_requests.c.status == "pending")
    if guild_id is not None:
        stmt = stmt.where(grant_requests.c.guild_id == guild_id)
    await conn.execute(stmt.values(next_poll_at=func.now()))


async def postpone(conn: AsyncConnection, request_id: uuid.UUID, until: datetime) -> None:
    await conn.execute(update(grant_requests).where(grant_requests.c.id == request_id).values(next_poll_at=until))


async def finish_request(conn: AsyncConnection, request: GrantRequestRow, status: str, error: str | None = None) -> None:
    """End a request without a token: expired, or failed with the reason."""
    result = await conn.execute(update(grant_requests).where(and_(grant_requests.c.id == request.id, grant_requests.c.status == "pending")).values(status=status, error=error, completed_at=func.now()))
    if result.rowcount == 1 and status == "failed":
        await enqueue_dm(conn, request.requested_by, TEMPLATE_GRANT_FAILED, guild_id=str(request.guild_id))


async def approve_request(
    conn: AsyncConnection, request: GrantRequestRow, *, grant_id: str | None, unit: str, access_token_enc: str, access_expires_at: datetime, refresh_token_enc: str | None
) -> bool:
    """Store the guild token from an approved request and switch the guild's rewards to issuing in its currency; False when the request was already settled."""
    result = await conn.execute(update(grant_requests).where(and_(grant_requests.c.id == request.id, grant_requests.c.status == "pending")).values(status="approved", completed_at=func.now()))
    if result.rowcount != 1:
        return False
    values = {
        "grant_id": grant_id,
        "unit": unit,
        "access_token_enc": access_token_enc,
        "access_expires_at": access_expires_at,
        "refresh_token_enc": refresh_token_enc,
        "status": "active",
    }
    stmt = pg_insert(issue_grants).values(guild_id=request.guild_id, **values)
    await conn.execute(stmt.on_conflict_do_update(index_elements=["guild_id"], set_={**values, "updated_at": func.now()}))
    await guilds.set_funding_source(conn, request.guild_id, "issue", reward_unit=unit)
    await enqueue_dm(conn, request.requested_by, TEMPLATE_GRANT_APPROVED, guild_id=str(request.guild_id), unit=unit)
    return True


async def active_grant(conn: AsyncConnection, guild_id: int, *, lock: bool = False) -> Grant | None:
    stmt = select(issue_grants).where(and_(issue_grants.c.guild_id == guild_id, issue_grants.c.status == "active"))
    if lock:
        stmt = stmt.with_for_update()
    row = (await conn.execute(stmt)).first()
    return _grant(row) if row else None


async def expiring(conn: AsyncConnection, before: datetime) -> list[Grant]:
    rows = await conn.execute(select(issue_grants).where(and_(issue_grants.c.status == "active", issue_grants.c.access_expires_at <= before, issue_grants.c.refresh_token_enc.is_not(None))))
    return [_grant(r) for r in rows]


async def store_tokens(conn: AsyncConnection, guild_id: int, *, access_token_enc: str, access_expires_at: datetime, refresh_token_enc: str | None) -> None:
    await conn.execute(
        update(issue_grants)
        .where(issue_grants.c.guild_id == guild_id)
        .values(access_token_enc=access_token_enc, access_expires_at=access_expires_at, refresh_token_enc=refresh_token_enc, updated_at=func.now())
    )


async def revoke(conn: AsyncConnection, guild_id: int, *, grant_id: str | None = None) -> bool:
    """Mark the guild's grant revoked (only the named grant when grant_id is given) and tell whoever requested it; rewards stop until a new grant is approved."""
    stmt = update(issue_grants).where(and_(issue_grants.c.guild_id == guild_id, issue_grants.c.status == "active"))
    if grant_id is not None:
        stmt = stmt.where(issue_grants.c.grant_id == grant_id)
    result = await conn.execute(stmt.values(status="revoked", updated_at=func.now()))
    if result.rowcount != 1:
        return False
    requester = await conn.scalar(
        select(grant_requests.c.requested_by).where(and_(grant_requests.c.guild_id == guild_id, grant_requests.c.status == "approved")).order_by(grant_requests.c.completed_at.desc()).limit(1)
    )
    if requester is not None:
        await enqueue_dm(conn, int(requester), TEMPLATE_GRANT_REVOKED, guild_id=str(guild_id))
    return True


async def grant_status(conn: AsyncConnection, guild_id: int) -> str | None:
    return await conn.scalar(select(issue_grants.c.status).where(issue_grants.c.guild_id == guild_id))
