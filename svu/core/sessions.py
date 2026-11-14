"""Dashboard login sessions and OAuth states, stored under the digest of their bearer value."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from svu.crypto import new_token, token_digest
from svu.db.tables import dashboard_sessions, oauth_states

SESSION_LIFETIME = timedelta(days=7)
STATE_LIFETIME = timedelta(minutes=10)


@dataclass(frozen=True)
class Session:
    token_digest: str
    user_id: int
    username: str
    avatar: str | None
    discord_token_enc: str
    guilds: list[dict[str, Any]] | None
    guilds_fetched_at: datetime | None
    csrf_token: str
    flash: dict[str, Any] | None
    expires_at: datetime


def _session(row: Any) -> Session:
    return Session(**{name: getattr(row, name) for name in Session.__dataclass_fields__})


async def create_state(conn: AsyncConnection, next_path: str) -> str:
    state = new_token()
    await conn.execute(insert(oauth_states).values(state_digest=token_digest(state), next_path=next_path))
    return state


async def consume_state(conn: AsyncConnection, state: str, now: datetime) -> str | None:
    """The path to return to, when state was issued by create_state within STATE_LIFETIME; a state works once."""
    row = (await conn.execute(delete(oauth_states).where(oauth_states.c.state_digest == token_digest(state)).returning(oauth_states))).first()
    if row is None or now - row.created_at > STATE_LIFETIME:
        return None
    return str(row.next_path)


async def create(conn: AsyncConnection, *, user_id: int, username: str, avatar: str | None, discord_token_enc: str, now: datetime) -> str:
    """Start a session and return its bearer token, which only the cookie holds."""
    token = new_token()
    await conn.execute(
        insert(dashboard_sessions).values(
            token_digest=token_digest(token), user_id=user_id, username=username, avatar=avatar, discord_token_enc=discord_token_enc, csrf_token=new_token(), expires_at=now + SESSION_LIFETIME
        )
    )
    return token


async def get(conn: AsyncConnection, token: str, now: datetime) -> Session | None:
    row = (await conn.execute(select(dashboard_sessions).where(dashboard_sessions.c.token_digest == token_digest(token)))).first()
    if row is None or row.expires_at <= now:
        return None
    return _session(row)


async def store_guilds(conn: AsyncConnection, digest: str, guild_list: list[dict[str, Any]]) -> None:
    await conn.execute(update(dashboard_sessions).where(dashboard_sessions.c.token_digest == digest).values(guilds=guild_list, guilds_fetched_at=func.now()))


async def set_flash(conn: AsyncConnection, digest: str, level: str, message: str) -> None:
    await conn.execute(update(dashboard_sessions).where(dashboard_sessions.c.token_digest == digest).values(flash={"level": level, "message": message}))


async def pop_flash(conn: AsyncConnection, digest: str) -> dict[str, Any] | None:
    old = await conn.scalar(select(dashboard_sessions.c.flash).where(dashboard_sessions.c.token_digest == digest).with_for_update())
    if old is not None:
        await conn.execute(update(dashboard_sessions).where(dashboard_sessions.c.token_digest == digest).values(flash=None))
    return dict(old) if old else None


async def end(conn: AsyncConnection, digest: str) -> None:
    await conn.execute(delete(dashboard_sessions).where(dashboard_sessions.c.token_digest == digest))
