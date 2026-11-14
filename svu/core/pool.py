"""Reward pool balances: each guild's share of the bot's VirtualCrypto wallet, per unit, with every change recorded in reward_pool_entries."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from svu.db.tables import reward_pool_balances, reward_pool_entries


@dataclass(frozen=True)
class PoolBalance:
    unit: str
    balance: int


@dataclass(frozen=True)
class PoolEntry:
    id: int
    unit: str
    delta: int
    kind: str
    actor_id: int | None
    created_at: datetime


async def credit(
    conn: AsyncConnection, guild_id: int, unit: str, amount: int, kind: str, *, actor_id: int | None = None, operation_id: uuid.UUID | None = None, payout_id: uuid.UUID | None = None
) -> None:
    """Add amount to the guild's balance in unit, creating the balance row when needed."""
    stmt = pg_insert(reward_pool_balances).values(guild_id=guild_id, unit=unit, balance=amount)
    await conn.execute(stmt.on_conflict_do_update(index_elements=["guild_id", "unit"], set_={"balance": reward_pool_balances.c.balance + amount}))
    await conn.execute(insert(reward_pool_entries).values(guild_id=guild_id, unit=unit, delta=amount, kind=kind, actor_id=actor_id, operation_id=operation_id, payout_id=payout_id))


async def debit(conn: AsyncConnection, guild_id: int, unit: str, amount: int, kind: str, *, actor_id: int | None = None, payout_id: uuid.UUID | None = None) -> bool:
    """Subtract amount when the balance covers it and return True; return False and change nothing otherwise."""
    result = await conn.execute(
        update(reward_pool_balances)
        .where(and_(reward_pool_balances.c.guild_id == guild_id, reward_pool_balances.c.unit == unit, reward_pool_balances.c.balance >= amount))
        .values(balance=reward_pool_balances.c.balance - amount)
    )
    if result.rowcount != 1:
        return False
    await conn.execute(insert(reward_pool_entries).values(guild_id=guild_id, unit=unit, delta=-amount, kind=kind, actor_id=actor_id, payout_id=payout_id))
    return True


async def balances(conn: AsyncConnection, guild_id: int) -> list[PoolBalance]:
    rows = await conn.execute(select(reward_pool_balances.c.unit, reward_pool_balances.c.balance).where(reward_pool_balances.c.guild_id == guild_id).order_by(reward_pool_balances.c.unit))
    return [PoolBalance(unit=r.unit, balance=r.balance) for r in rows]


async def balance(conn: AsyncConnection, guild_id: int, unit: str) -> int:
    value = await conn.scalar(select(reward_pool_balances.c.balance).where(reward_pool_balances.c.guild_id == guild_id, reward_pool_balances.c.unit == unit))
    return int(value or 0)


async def entries(conn: AsyncConnection, guild_id: int, limit: int = 50) -> list[PoolEntry]:
    rows = await conn.execute(select(reward_pool_entries).where(reward_pool_entries.c.guild_id == guild_id).order_by(reward_pool_entries.c.id.desc()).limit(limit))
    return [PoolEntry(id=r.id, unit=r.unit, delta=r.delta, kind=r.kind, actor_id=r.actor_id, created_at=r.created_at) for r in rows]
