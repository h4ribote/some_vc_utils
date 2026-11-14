"""Claim-funded operations (rain, send, deposit): created before the claim exists, settled by whichever of the webhook, the reconciler or the expiry job sees the claim's outcome first."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from svu.core import pool
from svu.core.messages import enqueue_operation_update
from svu.db.tables import operations, payouts

# Recipients of a rain are paid in bulk requests of at most this many payments.
BATCH_SIZE = 100

OPEN_STATUSES = ("creating", "awaiting_claim")
FINAL_STATUSES = ("done", "partial", "failed", "denied", "canceled", "expired")
# The claim statuses a type 2 webhook or a claim read can report, mapped to the operation status they end an open operation in.
CLAIM_OUTCOMES = {"denied": "denied", "canceled": "canceled"}


@dataclass(frozen=True)
class InteractionRef:
    """The interaction whose original response shows an operation, editable with its token until it expires."""

    application_id: int
    token_enc: str
    expires_at: datetime


@dataclass(frozen=True)
class Operation:
    id: uuid.UUID
    kind: str
    status: str
    guild_id: int | None
    channel_id: int | None
    requester_id: int
    unit: str
    amount: int
    claim_id: int | None
    details: dict[str, Any]
    interaction_app_id: int | None
    interaction_token_enc: str | None
    interaction_expires_at: datetime | None
    created_at: datetime
    expires_at: datetime
    resolved_at: datetime | None
    error: str | None

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_STATUSES


def _operation(row: Any) -> Operation:
    return Operation(**{name: getattr(row, name) for name in Operation.__dataclass_fields__})


async def create(
    conn: AsyncConnection,
    *,
    kind: str,
    requester_id: int,
    unit: str,
    recipients: Sequence[tuple[int, int]] = (),
    deposit_amount: int | None = None,
    guild_id: int | None = None,
    channel_id: int | None = None,
    details: dict[str, Any] | None = None,
    interaction: InteractionRef | None = None,
    timeout_seconds: int,
) -> uuid.UUID:
    """Record an operation in status creating, with one held payout per (receiver, amount) recipient; its claim amount is the recipients' total, or deposit_amount for a deposit."""
    if kind == "deposit":
        if recipients or deposit_amount is None or deposit_amount <= 0:
            raise ValueError("a deposit has a positive amount and no recipients")
        amount = deposit_amount
    else:
        if not recipients or deposit_amount is not None or any(a <= 0 for _, a in recipients):
            raise ValueError("a rain or send pays one or more recipients positive amounts")
        amount = sum(a for _, a in recipients)
    op_id = uuid.uuid4()
    now = datetime.now(UTC)
    await conn.execute(
        insert(operations).values(
            id=op_id,
            kind=kind,
            status="creating",
            guild_id=guild_id,
            channel_id=channel_id,
            requester_id=requester_id,
            unit=unit,
            amount=amount,
            details=details or {},
            interaction_app_id=interaction.application_id if interaction else None,
            interaction_token_enc=interaction.token_enc if interaction else None,
            interaction_expires_at=interaction.expires_at if interaction else None,
            created_at=now,
            expires_at=now + timedelta(seconds=timeout_seconds),
        )
    )
    if recipients:
        await conn.execute(
            insert(payouts),
            [
                {"id": uuid.uuid4(), "operation_id": op_id, "guild_id": guild_id, "receiver_id": receiver, "unit": unit, "amount": amt, "method": "pay", "purpose": kind, "status": "held"}
                for receiver, amt in recipients
            ],
        )
    return op_id


async def get(conn: AsyncConnection, operation_id: uuid.UUID, *, lock: bool = False) -> Operation | None:
    stmt = select(operations).where(operations.c.id == operation_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await conn.execute(stmt)).first()
    return _operation(row) if row else None


async def get_by_claim(conn: AsyncConnection, claim_id: int, *, lock: bool = False) -> Operation | None:
    stmt = select(operations).where(operations.c.claim_id == claim_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await conn.execute(stmt)).first()
    return _operation(row) if row else None


async def attach_claim(conn: AsyncConnection, operation_id: uuid.UUID, claim_id: int) -> bool:
    """Move a creating operation to awaiting_claim with its claim; False when the operation already moved on, for example because the reconciler found the claim first."""
    result = await conn.execute(update(operations).where(and_(operations.c.id == operation_id, operations.c.status == "creating")).values(claim_id=claim_id, status="awaiting_claim"))
    if result.rowcount == 1:
        await enqueue_operation_update(conn, operation_id)
        return True
    return False


async def _close(conn: AsyncConnection, op: Operation, status: str, error: str | None = None) -> None:
    await conn.execute(update(operations).where(operations.c.id == op.id).values(status=status, resolved_at=func.now(), error=error))
    await conn.execute(update(payouts).where(and_(payouts.c.operation_id == op.id, payouts.c.status == "held")).values(status="canceled", completed_at=func.now()))
    await enqueue_operation_update(conn, op.id)


async def fail_creation(conn: AsyncConnection, operation_id: uuid.UUID, error: str) -> bool:
    """End a creating operation whose claim could not be made; False when it is no longer creating."""
    op = await get(conn, operation_id, lock=True)
    if op is None or op.status != "creating":
        return False
    await _close(conn, op, "failed", error)
    return True


async def apply_claim_status(conn: AsyncConnection, op: Operation, claim_status: str, *, expired: bool = False) -> bool:
    """Apply a claim outcome to an open operation locked by the caller; returns whether anything changed, so repeated or late reports are harmless."""
    if not op.is_open or claim_status == "pending":
        return False
    if claim_status == "approved":
        await _approve(conn, op)
        return True
    if claim_status not in CLAIM_OUTCOMES:
        raise ValueError(f"unknown claim status {claim_status!r}")
    await _close(conn, op, "expired" if expired and claim_status == "canceled" else CLAIM_OUTCOMES[claim_status])
    return True


async def apply_claim(conn: AsyncConnection, claim_id: int, claim_status: str, *, operation_hint: uuid.UUID | None = None) -> bool:
    """Apply a claim outcome reported by VirtualCrypto; operation_hint (the claim's svu_operation metadata) finds an operation whose claim id was never recorded."""
    op = await get_by_claim(conn, claim_id, lock=True)
    if op is None and operation_hint is not None:
        op = await get(conn, operation_hint, lock=True)
        if op is None or op.status != "creating":
            return False
        await conn.execute(update(operations).where(operations.c.id == op.id).values(claim_id=claim_id, status="awaiting_claim"))
        op = await get(conn, op.id, lock=True)
        assert op is not None
        if claim_status == "pending":
            await enqueue_operation_update(conn, op.id)
            return True
    if op is None:
        return False
    return await apply_claim_status(conn, op, claim_status)


async def _approve(conn: AsyncConnection, op: Operation) -> None:
    if op.kind == "deposit":
        if op.guild_id is None:
            raise ValueError("a deposit belongs to a guild")
        await pool.credit(conn, op.guild_id, op.unit, op.amount, "deposit", actor_id=op.requester_id, operation_id=op.id)
        await conn.execute(update(operations).where(operations.c.id == op.id).values(status="done", resolved_at=func.now()))
        await enqueue_operation_update(conn, op.id)
        return
    held = (await conn.execute(select(payouts.c.id).where(and_(payouts.c.operation_id == op.id, payouts.c.status == "held")).order_by(payouts.c.id))).scalars().all()
    use_batches = op.kind == "rain" and len(held) > 1
    for start in range(0, len(held), BATCH_SIZE):
        chunk = held[start : start + BATCH_SIZE]
        batch_id = uuid.uuid4() if use_batches and len(chunk) > 1 else None
        await conn.execute(update(payouts).where(payouts.c.id.in_(chunk)).values(status="pending", batch_id=batch_id, next_attempt_at=func.now()))
    await conn.execute(update(operations).where(operations.c.id == op.id).values(status="paying"))
    await enqueue_operation_update(conn, op.id)


async def settle(conn: AsyncConnection, operation_id: uuid.UUID) -> None:
    """Finish a paying operation once none of its own payouts is still held or pending: done, partial or failed by how many were paid; a partial one is re-evaluated when an unknown payout is resolved."""
    op = await get(conn, operation_id, lock=True)
    if op is None or op.status not in ("paying", "partial"):
        return
    rows = await conn.execute(select(payouts.c.status, func.count()).where(and_(payouts.c.operation_id == op.id, payouts.c.purpose != "refund")).group_by(payouts.c.status))
    counts = {status: count for status, count in rows}
    if counts.get("held", 0) or counts.get("pending", 0):
        return
    paid = counts.get("done", 0)
    unresolved = counts.get("unknown", 0)
    total = sum(counts.values())
    if paid == total:
        status = "done"
    elif paid == 0 and unresolved == 0:
        status = "failed"
    else:
        status = "partial"
    if status == op.status:
        return
    await conn.execute(update(operations).where(operations.c.id == op.id).values(status=status, resolved_at=func.now()))
    await enqueue_operation_update(conn, op.id)


async def open_operations(conn: AsyncConnection, status: str, *, older_than: datetime | None = None, limit: int = 100) -> list[Operation]:
    stmt = select(operations).where(operations.c.status == status)
    if older_than is not None:
        stmt = stmt.where(operations.c.created_at < older_than)
    rows = await conn.execute(stmt.order_by(operations.c.created_at).limit(limit))
    return [_operation(r) for r in rows]


async def expired_awaiting(conn: AsyncConnection, now: datetime, limit: int = 100) -> list[Operation]:
    rows = await conn.execute(select(operations).where(and_(operations.c.status == "awaiting_claim", operations.c.expires_at <= now)).order_by(operations.c.expires_at).limit(limit))
    return [_operation(r) for r in rows]


async def recent(conn: AsyncConnection, *, guild_id: int | None = None, requester_id: int | None = None, limit: int = 50) -> list[Operation]:
    stmt = select(operations)
    if guild_id is not None:
        stmt = stmt.where(operations.c.guild_id == guild_id)
    if requester_id is not None:
        stmt = stmt.where(operations.c.requester_id == requester_id)
    rows = await conn.execute(stmt.order_by(operations.c.created_at.desc()).limit(limit))
    return [_operation(r) for r in rows]
