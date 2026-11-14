"""Reward reservation: in one transaction the rule, the cooldown and the funding are checked, the cooldown is recorded, the pool is debited and a payout is queued."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from svu.core import grants, guilds, pool
from svu.db.tables import payouts, reward_cooldowns, reward_rules


@dataclass(frozen=True)
class Reservation:
    payout_id: uuid.UUID
    unit: str
    amount: int
    method: str


async def reserve(conn: AsyncConnection, guild_id: int, user_id: int, reward_type: str, now: datetime) -> Reservation | None:
    """Queue the reward due to user_id for reward_type, or return None when there is no rule, the cooldown is running or the funding cannot cover it."""
    rule = (await conn.execute(select(reward_rules).where(and_(reward_rules.c.guild_id == guild_id, reward_rules.c.reward_type == reward_type)))).first()
    if rule is None:
        return None
    settings = await guilds.settings(conn, guild_id)
    if settings.funding_source == "issue":
        grant = await grants.active_grant(conn, guild_id)
        if grant is None:
            return None
        unit, method = grant.unit, "issue"
    else:
        if settings.reward_unit is None:
            return None
        unit, method = settings.reward_unit, "pay"

    # The insert creates the row the lock below needs, so two events for the same user serialize on it even when neither had a cooldown yet.
    await conn.execute(
        pg_insert(reward_cooldowns)
        .values(guild_id=guild_id, user_id=user_id, reward_type=reward_type, last_rewarded_at=datetime.fromtimestamp(0, now.tzinfo))
        .on_conflict_do_nothing(index_elements=["guild_id", "user_id", "reward_type"])
    )
    cooldown_row = (
        await conn.execute(
            select(reward_cooldowns.c.last_rewarded_at)
            .where(and_(reward_cooldowns.c.guild_id == guild_id, reward_cooldowns.c.user_id == user_id, reward_cooldowns.c.reward_type == reward_type))
            .with_for_update()
        )
    ).one()
    previous: datetime = cooldown_row.last_rewarded_at
    if now - previous < timedelta(seconds=rule.cooldown_seconds):
        return None

    payout_id = uuid.uuid4()
    if method == "pay" and not await pool.debit(conn, guild_id, unit, rule.amount, "reward", payout_id=payout_id):
        return None
    await conn.execute(
        update(reward_cooldowns).where(and_(reward_cooldowns.c.guild_id == guild_id, reward_cooldowns.c.user_id == user_id, reward_cooldowns.c.reward_type == reward_type)).values(last_rewarded_at=now)
    )
    await conn.execute(
        insert(payouts).values(
            id=payout_id,
            guild_id=guild_id,
            receiver_id=user_id,
            unit=unit,
            amount=rule.amount,
            method=method,
            purpose="reward",
            reward_type=reward_type,
            cooldown_at=now,
            cooldown_prev=None if previous.timestamp() == 0 else previous,
            status="pending",
        )
    )
    return Reservation(payout_id=payout_id, unit=unit, amount=rule.amount, method=method)


async def withdraw(conn: AsyncConnection, guild_id: int, unit: str, amount: int, actor_id: int) -> uuid.UUID | None:
    """Move amount of the guild's pool to actor_id's wallet; None when the balance does not cover it."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    payout_id = uuid.uuid4()
    if not await pool.debit(conn, guild_id, unit, amount, "withdraw", actor_id=actor_id, payout_id=payout_id):
        return None
    await conn.execute(insert(payouts).values(id=payout_id, guild_id=guild_id, receiver_id=actor_id, unit=unit, amount=amount, method="pay", purpose="withdraw", status="pending"))
    return payout_id
