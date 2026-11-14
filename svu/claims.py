"""Starting a claim-funded operation, shared by the bot and the dashboard: the operation row is committed before the claim is requested, so no claim ever exists without one."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from svu.core import operations
from svu.core.operations import InteractionRef, Operation
from svu.core.webhooks import OPERATION_METADATA_KEY
from svu.vc import VCClient, VCError, VCRequestError

log = logging.getLogger("svu.claims")

VC_ERROR_TEXT = {
    "not_found_currency": "その通貨は存在しません。",
    "invalid_amount": "金額が正しくありません。",
    "not_enough_amount": "残高が足りません。",
    "invalid_payer_discord_id_value": "支払う人のIDが正しくありません。",
}


def describe_vc_error(exc: VCError) -> str:
    """A message for the person who asked, naming VirtualCrypto's reason when it gave one this module knows."""
    if exc.code in VC_ERROR_TEXT:
        return VC_ERROR_TEXT[exc.code]
    if exc.definitive:
        return f"VirtualCryptoが要求を拒否しました ({exc.code or exc.status})。"
    return "VirtualCryptoに接続できませんでした。しばらくしてからもう一度お試しください。"


async def start_operation(
    engine: AsyncEngine,
    vc: VCClient,
    *,
    kind: str,
    requester_id: int,
    unit: str,
    timeout_seconds: int,
    recipients: Sequence[tuple[int, int]] = (),
    deposit_amount: int | None = None,
    guild_id: int | None = None,
    channel_id: int | None = None,
    details: dict[str, Any] | None = None,
    interaction: InteractionRef | None = None,
) -> Operation:
    """Create the operation, request its claim from the requester and return the operation's state afterwards.

    A refused claim fails the operation with the reason; an unanswered request leaves it creating for the worker to find the claim by its metadata or fail it.
    """
    async with engine.begin() as conn:
        op_id = await operations.create(
            conn,
            kind=kind,
            requester_id=requester_id,
            unit=unit,
            recipients=recipients,
            deposit_amount=deposit_amount,
            guild_id=guild_id,
            channel_id=channel_id,
            details=details,
            interaction=interaction,
            timeout_seconds=timeout_seconds,
        )
        op = await operations.get(conn, op_id)
    assert op is not None
    try:
        claim = await vc.create_claim(requester_id, unit, op.amount, {OPERATION_METADATA_KEY: str(op_id)})
    except VCRequestError as exc:
        async with engine.begin() as conn:
            await operations.fail_creation(conn, op_id, describe_vc_error(exc))
    except VCError as exc:
        log.warning("claim creation unanswered; the worker will reconcile it", extra={"operation_id": str(op_id), "reason": str(exc)})
    else:
        async with engine.begin() as conn:
            await operations.attach_claim(conn, op_id, claim.id)
    async with engine.connect() as conn:
        current = await operations.get(conn, op_id)
    assert current is not None
    return current
