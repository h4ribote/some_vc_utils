"""The payout outbox: every transfer the bot makes is a row first, sent by the worker under an idempotency key derived from the row, and compensated only when VirtualCrypto refused it."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from svu.core import operations, pool
from svu.core.messages import TEMPLATE_SEND_RECEIVED, enqueue_dm
from svu.db.tables import payouts, reward_cooldowns, users

# A payout being sent is not handed out again for this long, so a crashed worker's payouts are retried with the same key afterwards.
SEND_LEASE = timedelta(seconds=120)
BACKOFF_BASE_SECONDS = 5
BACKOFF_MAX_SECONDS = 3600
# Past this age a payout whose outcome is still unknown is parked for an administrator, before VirtualCrypto forgets its idempotency key.
UNKNOWN_AFTER = timedelta(days=5)


@dataclass(frozen=True)
class Payout:
    id: uuid.UUID
    operation_id: uuid.UUID | None
    refund_of: uuid.UUID | None
    guild_id: int | None
    receiver_id: int
    unit: str
    amount: int
    method: str
    purpose: str
    reward_type: str | None
    cooldown_at: datetime | None
    cooldown_prev: datetime | None
    batch_id: uuid.UUID | None
    status: str
    attempts: int
    last_error: str | None
    next_attempt_at: datetime
    created_at: datetime
    completed_at: datetime | None


def _payout(row: Any) -> Payout:
    return Payout(**{name: getattr(row, name) for name in Payout.__dataclass_fields__})


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_MAX_SECONDS))


async def get(conn: AsyncConnection, payout_id: uuid.UUID, *, lock: bool = False) -> Payout | None:
    stmt = select(payouts).where(payouts.c.id == payout_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await conn.execute(stmt)).first()
    return _payout(row) if row else None


async def take_due(conn: AsyncConnection, now: datetime) -> list[Payout]:
    """Lease the next due unit of work: one payout, or every pending payout of the same batch; empty when nothing is due."""
    first = (
        await conn.execute(
            select(payouts).where(and_(payouts.c.status == "pending", payouts.c.next_attempt_at <= now)).order_by(payouts.c.next_attempt_at, payouts.c.id).limit(1).with_for_update(skip_locked=True)
        )
    ).first()
    if first is None:
        return []
    if first.batch_id is None:
        rows = [first]
    else:
        rows = list(await conn.execute(select(payouts).where(and_(payouts.c.batch_id == first.batch_id, payouts.c.status == "pending")).order_by(payouts.c.id).with_for_update(skip_locked=True)))
    ids = [r.id for r in rows]
    await conn.execute(update(payouts).where(payouts.c.id.in_(ids)).values(attempts=payouts.c.attempts + 1, next_attempt_at=now + SEND_LEASE))
    return [_payout(r) for r in rows]


async def mark_done(conn: AsyncConnection, payout_ids: Sequence[uuid.UUID]) -> None:
    rows = (
        await conn.execute(
            update(payouts).where(and_(payouts.c.id.in_(payout_ids), payouts.c.status.in_(("pending", "unknown")))).values(status="done", completed_at=func.now(), last_error=None).returning(payouts)
        )
    ).all()
    for row in rows:
        p = _payout(row)
        if p.purpose == "send" and p.operation_id is not None:
            wants_dm = await conn.scalar(select(users.c.receive_dm).where(users.c.discord_id == p.receiver_id))
            if wants_dm:
                await enqueue_dm(conn, p.receiver_id, TEMPLATE_SEND_RECEIVED, operation_id=str(p.operation_id))
    for op_id in {r.operation_id for r in rows if r.operation_id is not None}:
        await operations.settle(conn, op_id)


async def mark_retry(conn: AsyncConnection, payout_ids: Sequence[uuid.UUID], error: str, now: datetime) -> None:
    """Schedule another attempt with the same key; payouts older than UNKNOWN_AFTER become unknown and wait for an administrator."""
    rows = (await conn.execute(select(payouts).where(and_(payouts.c.id.in_(payout_ids), payouts.c.status == "pending")))).all()
    for row in rows:
        if now - row.created_at > UNKNOWN_AFTER:
            await conn.execute(update(payouts).where(payouts.c.id == row.id).values(status="unknown", last_error=error))
        else:
            await conn.execute(update(payouts).where(payouts.c.id == row.id).values(last_error=error, next_attempt_at=now + backoff(row.attempts)))
    for op_id in {r.operation_id for r in rows if r.operation_id is not None}:
        await operations.settle(conn, op_id)


async def unbatch(conn: AsyncConnection, batch_id: uuid.UUID, error: str) -> None:
    """Send a refused bulk payment's rows one by one, so a single bad receiver does not fail the rest."""
    await conn.execute(update(payouts).where(and_(payouts.c.batch_id == batch_id, payouts.c.status == "pending")).values(batch_id=None, last_error=error, next_attempt_at=func.now()))


async def mark_failed(conn: AsyncConnection, payout_id: uuid.UUID, error: str) -> None:
    """Record that VirtualCrypto refused the payout, then compensate: a reward or withdrawal goes back to the pool, a rain or send share is refunded to its requester."""
    row = (
        await conn.execute(
            update(payouts).where(and_(payouts.c.id == payout_id, payouts.c.status.in_(("pending", "unknown")))).values(status="failed", completed_at=func.now(), last_error=error).returning(payouts)
        )
    ).first()
    if row is None:
        return
    p = _payout(row)
    if p.purpose == "reward":
        if p.method == "pay" and p.guild_id is not None:
            await pool.credit(conn, p.guild_id, p.unit, p.amount, "reward_release", payout_id=p.id)
        await _restore_cooldown(conn, p)
    elif p.purpose == "withdraw" and p.guild_id is not None:
        await pool.credit(conn, p.guild_id, p.unit, p.amount, "withdraw_release", actor_id=p.receiver_id, payout_id=p.id)
    elif p.purpose in ("rain", "send") and p.operation_id is not None:
        op = await operations.get(conn, p.operation_id)
        if op is not None:
            await conn.execute(
                insert(payouts).values(
                    id=uuid.uuid4(),
                    operation_id=op.id,
                    refund_of=p.id,
                    guild_id=op.guild_id,
                    receiver_id=op.requester_id,
                    unit=p.unit,
                    amount=p.amount,
                    method="pay",
                    purpose="refund",
                    status="pending",
                )
            )
    if p.operation_id is not None:
        await operations.settle(conn, p.operation_id)


async def _restore_cooldown(conn: AsyncConnection, p: Payout) -> None:
    """Undo the cooldown the reservation wrote, unless a later reward has replaced it."""
    if p.guild_id is None or p.reward_type is None or p.cooldown_at is None:
        return
    where = and_(
        reward_cooldowns.c.guild_id == p.guild_id,
        reward_cooldowns.c.user_id == p.receiver_id,
        reward_cooldowns.c.reward_type == p.reward_type,
        reward_cooldowns.c.last_rewarded_at == p.cooldown_at,
    )
    if p.cooldown_prev is None:
        await conn.execute(reward_cooldowns.delete().where(where))
    else:
        await conn.execute(update(reward_cooldowns).where(where).values(last_rewarded_at=p.cooldown_prev))


async def for_operation(conn: AsyncConnection, operation_id: uuid.UUID) -> list[Payout]:
    rows = await conn.execute(select(payouts).where(payouts.c.operation_id == operation_id).order_by(payouts.c.created_at, payouts.c.id))
    return [_payout(r) for r in rows]


async def recent(conn: AsyncConnection, *, guild_id: int | None = None, statuses: Sequence[str] | None = None, limit: int = 50) -> list[Payout]:
    stmt = select(payouts)
    if guild_id is not None:
        stmt = stmt.where(payouts.c.guild_id == guild_id)
    if statuses is not None:
        stmt = stmt.where(payouts.c.status.in_(statuses))
    rows = await conn.execute(stmt.order_by(payouts.c.created_at.desc()).limit(limit))
    return [_payout(r) for r in rows]


async def needing_attention(conn: AsyncConnection, limit: int = 100) -> list[Payout]:
    """Payouts an administrator has to resolve: outcomes unknown, and refunds VirtualCrypto refused."""
    rows = await conn.execute(select(payouts).where(or_(payouts.c.status == "unknown", and_(payouts.c.status == "failed", payouts.c.purpose == "refund"))).order_by(payouts.c.created_at).limit(limit))
    return [_payout(r) for r in rows]


async def resolve_unknown(conn: AsyncConnection, payout_id: uuid.UUID, outcome: str) -> bool:
    """Settle an unknown payout after checking VirtualCrypto by hand: outcome "paid" or "not_paid" (which compensates)."""
    p = await get(conn, payout_id, lock=True)
    if p is None or p.status != "unknown":
        return False
    if outcome == "paid":
        await mark_done(conn, [payout_id])
    elif outcome == "not_paid":
        await mark_failed(conn, payout_id, "resolved by an administrator as not paid")
    else:
        raise ValueError(f"unknown outcome {outcome!r}")
    return True


def utcnow() -> datetime:
    return datetime.now(UTC)
