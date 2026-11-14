from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from svu import render
from svu.core.operations import Operation
from svu.core.payouts import Payout

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def op(status: str, kind: str = "rain", **changes: Any) -> Operation:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "kind": kind,
        "status": status,
        "guild_id": 1,
        "channel_id": 2,
        "requester_id": 3,
        "unit": "nyan",
        "amount": 300,
        "claim_id": 77,
        "details": {"role_id": "9", "member_count": 3, "amount_per_user": 100, "receiver_id": "4", "message": "hi"},
        "interaction_app_id": None,
        "interaction_token_enc": None,
        "interaction_expires_at": None,
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
        "resolved_at": None,
        "error": None,
    }
    values.update(changes)
    return Operation(**values)


def payout(status: str, receiver: int, purpose: str = "rain", amount: int = 100) -> Payout:
    return Payout(
        id=uuid.uuid4(),
        operation_id=None,
        refund_of=None,
        guild_id=1,
        receiver_id=receiver,
        unit="nyan",
        amount=amount,
        method="pay",
        purpose=purpose,
        reward_type=None,
        cooldown_at=None,
        cooldown_prev=None,
        batch_id=None,
        status=status,
        attempts=0,
        last_error=None,
        next_attempt_at=NOW,
        created_at=NOW,
        completed_at=None,
    )


def fields(embed: Any) -> dict[str, str]:
    return {f.name: f.value for f in embed.fields}


def test_awaiting_claim_shows_how_to_approve() -> None:
    embed = render.operation_embed(op("awaiting_claim"))
    assert "/claim approve id:77" in fields(embed)["承認方法"]
    assert "<t:" in fields(embed)["期限"]


def test_partial_lists_failed_recipients_and_refunds() -> None:
    shares = [payout("done", 10), payout("failed", 11), payout("done", 3, purpose="refund")]
    embed = render.operation_embed(op("partial"), shares)
    values = fields(embed)
    assert "<@11>" in values["支払えなかった相手 (1人)"]
    assert "100 nyan を返金しました" in values["返金"]


def test_every_status_renders() -> None:
    for status in ("creating", "awaiting_claim", "paying", "done", "partial", "failed", "denied", "canceled", "expired"):
        for kind in ("rain", "send", "deposit"):
            assert render.operation_embed(op(status, kind=kind)).title


def test_operation_message_never_pings() -> None:
    message = render.operation_message(op("done"))
    assert message["allowed_mentions"] == {"parse": []}


def test_mention_list_fits_the_field_limit() -> None:
    text = render.mention_list(list(range(10**17, 10**17 + 200)))
    assert len(text) <= render.FIELD_LIMIT
    assert text.endswith("人")
    assert render.mention_list([1, 2]) == "<@1> <@2>"
