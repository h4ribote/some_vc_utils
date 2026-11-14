"""Claim-funded operations end to end: the claim, its outcome from any of the three sources, the payouts and the messages."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from svu.claims import start_operation
from svu.core import operations, payouts, pool, users, webhooks
from svu.core.operations import InteractionRef
from svu.crypto import SecretBox
from svu.db.tables import discord_messages
from svu.db.tables import operations as operations_table
from svu.vc import VCClient
from svu.worker.jobs import CREATING_GIVE_UP, CREATING_GRACE, Worker
from tests.fake_vc import APP_DISCORD_ID, FakeVC
from tests.integration.conftest import GUILD, REQUESTER, Clock, FakeDiscord

MEMBERS = [3_000_000_000_000_000_000 + i for i in range(3)]


async def run_all(worker: Worker) -> None:
    for _ in range(3):
        await worker.webhooks_tick()
        await worker.payouts_tick()
        await worker.messages_tick()


def interaction(box: SecretBox) -> InteractionRef:
    return InteractionRef(application_id=77, token_enc=box.encrypt("interaction-token"), expires_at=datetime.now(UTC) + timedelta(minutes=15))


async def start_rain(db: AsyncEngine, vc_client: VCClient, box: SecretBox, members: list[int] = MEMBERS, amount: int = 100) -> operations.Operation:
    return await start_operation(
        db,
        vc_client,
        kind="rain",
        requester_id=REQUESTER,
        unit="nyan",
        recipients=[(m, amount) for m in members],
        guild_id=GUILD,
        details={"role_id": "5", "member_count": len(members), "amount_per_user": amount},
        interaction=interaction(box),
        timeout_seconds=600,
    )


async def deliver(db: AsyncEngine, fake_vc: FakeVC, *claim_ids: int) -> None:
    async with db.begin() as conn:
        await webhooks.record(conn, 2, fake_vc.claim_event(*(fake_vc.claims[c] for c in claim_ids)))


async def test_rain_pays_every_member_once_after_the_webhook(db: AsyncEngine, vc_client: VCClient, fake_vc: FakeVC, worker: Worker, box: SecretBox, fake_discord: FakeDiscord) -> None:
    op = await start_rain(db, vc_client, box)
    assert op.status == "awaiting_claim" and op.claim_id is not None
    assert fake_vc.claims[op.claim_id].metadata == {"svu_operation": str(op.id)}
    fake_vc.approve(op.claim_id)
    await deliver(db, fake_vc, op.claim_id)
    await deliver(db, fake_vc, op.claim_id)
    await run_all(worker)
    async with db.connect() as conn:
        final = await operations.get(conn, op.id)
    assert final is not None and final.status == "done"
    assert [fake_vc.balance(m, "nyan") for m in MEMBERS] == [100, 100, 100]
    assert fake_vc.balance(APP_DISCORD_ID, "nyan") == 0
    assert fake_vc.payment_calls == 1, "three members are one bulk payment"
    assert fake_discord.edits and "完了" in fake_discord.edits[-1]["embeds"][0]["title"]


async def test_a_lost_payment_response_is_retried_with_the_same_key(db: AsyncEngine, vc_client: VCClient, fake_vc: FakeVC, worker: Worker, box: SecretBox) -> None:
    op = await start_rain(db, vc_client, box)
    assert op.claim_id is not None
    fake_vc.approve(op.claim_id)
    await deliver(db, fake_vc, op.claim_id)
    fake_vc.dropped.append("POST /api/v2/users/@me/transactions")
    await worker.webhooks_tick()
    await worker.payouts_tick()
    async with db.connect() as conn:
        pending = await payouts.for_operation(conn, op.id)
    assert {p.status for p in pending} == {"pending"} and all(p.last_error for p in pending)
    assert isinstance(worker.clock, Clock)
    worker.clock.advance(minutes=10)
    await worker.payouts_tick()
    assert [fake_vc.balance(m, "nyan") for m in MEMBERS] == [100, 100, 100]
    async with db.connect() as conn:
        final = await operations.get(conn, op.id)
    assert final is not None and final.status == "done"


async def test_a_refused_share_is_refunded_and_the_rest_paid(db: AsyncEngine, vc_client: VCClient, fake_vc: FakeVC, worker: Worker, box: SecretBox) -> None:
    op = await start_rain(db, vc_client, box, members=MEMBERS[:2])
    assert op.claim_id is not None
    fake_vc.approve(op.claim_id)
    await deliver(db, fake_vc, op.claim_id)
    refusal = {"error": "invalid_request", "error_description": "invalid_format_of_receiver_discord_id"}
    fake_vc.force("POST /api/v2/users/@me/transactions", 400, refusal)
    fake_vc.force("POST /api/v2/users/@me/transactions", 400, refusal)
    before = fake_vc.balance(REQUESTER, "nyan")
    await run_all(worker)
    async with db.connect() as conn:
        final = await operations.get(conn, op.id)
        rows = await payouts.for_operation(conn, op.id)
    assert final is not None and final.status == "partial"
    assert sorted(p.status for p in rows if p.purpose == "rain") == ["done", "failed"]
    [refund] = [p for p in rows if p.purpose == "refund"]
    assert refund.status == "done" and refund.receiver_id == REQUESTER
    assert fake_vc.balance(REQUESTER, "nyan") == before + 100


async def test_reconciler_settles_a_claim_whose_webhook_was_missed(db: AsyncEngine, vc_client: VCClient, fake_vc: FakeVC, worker: Worker, box: SecretBox) -> None:
    op = await start_rain(db, vc_client, box)
    assert op.claim_id is not None
    fake_vc.deny(op.claim_id)
    await worker.reconcile_tick()
    async with db.connect() as conn:
        final = await operations.get(conn, op.id)
        rows = await payouts.for_operation(conn, op.id)
    assert final is not None and final.status == "denied"
    assert {p.status for p in rows} == {"canceled"}


async def test_an_unapproved_claim_is_canceled_at_its_deadline(db: AsyncEngine, vc_client: VCClient, fake_vc: FakeVC, worker: Worker, box: SecretBox) -> None:
    op = await start_rain(db, vc_client, box)
    assert op.claim_id is not None
    assert isinstance(worker.clock, Clock)
    worker.clock.advance(minutes=11)
    await worker.expire_tick()
    async with db.connect() as conn:
        final = await operations.get(conn, op.id)
    assert final is not None and final.status == "expired"
    assert fake_vc.claims[op.claim_id].status == "canceled"


async def test_approval_racing_the_deadline_wins(db: AsyncEngine, vc_client: VCClient, fake_vc: FakeVC, worker: Worker, box: SecretBox) -> None:
    op = await start_rain(db, vc_client, box)
    assert op.claim_id is not None
    fake_vc.approve(op.claim_id)
    assert isinstance(worker.clock, Clock)
    worker.clock.advance(minutes=11)
    await worker.expire_tick()
    await worker.payouts_tick()
    async with db.connect() as conn:
        final = await operations.get(conn, op.id)
    assert final is not None and final.status == "done"


async def test_a_claim_created_without_its_answer_is_found_by_metadata(db: AsyncEngine, vc_client: VCClient, fake_vc: FakeVC, worker: Worker, box: SecretBox) -> None:
    fake_vc.dropped.append("POST /api/v2/users/@me/claims")
    op = await start_rain(db, vc_client, box)
    assert op.status == "creating" and op.claim_id is None
    [claim] = fake_vc.claims.values()
    assert isinstance(worker.clock, Clock)
    worker.clock.advance(seconds=CREATING_GRACE.total_seconds() + 1)
    async with db.begin() as conn:
        await conn.execute(update(operations_table).where(operations_table.c.id == op.id).values(created_at=worker.clock() - CREATING_GRACE - timedelta(seconds=1)))
    await worker.reconcile_tick()
    async with db.connect() as conn:
        found = await operations.get(conn, op.id)
    assert found is not None and found.status == "awaiting_claim" and found.claim_id == claim.id


async def test_a_claim_that_never_appears_fails_the_operation(db: AsyncEngine, vc_client: VCClient, fake_vc: FakeVC, worker: Worker, box: SecretBox) -> None:
    fake_vc.force("POST /api/v2/users/@me/claims", 503, {"error": "server_error"})
    op = await start_rain(db, vc_client, box)
    assert op.status == "creating"
    assert isinstance(worker.clock, Clock)
    async with db.begin() as conn:
        await conn.execute(update(operations_table).where(operations_table.c.id == op.id).values(created_at=worker.clock() - CREATING_GIVE_UP - timedelta(seconds=1)))
    await worker.reconcile_tick()
    async with db.connect() as conn:
        final = await operations.get(conn, op.id)
    assert final is not None and final.status == "failed"


async def test_a_refused_claim_fails_at_once_with_the_reason(db: AsyncEngine, vc_client: VCClient, box: SecretBox) -> None:
    op = await start_operation(db, vc_client, kind="send", requester_id=REQUESTER, unit="missing", recipients=[(5, 1)], timeout_seconds=600, interaction=interaction(box))
    assert op.status == "failed" and op.error == "その通貨は存在しません。"


async def test_deposit_credits_the_pool(db: AsyncEngine, vc_client: VCClient, fake_vc: FakeVC, worker: Worker) -> None:
    op = await start_operation(db, vc_client, kind="deposit", requester_id=REQUESTER, unit="nyan", deposit_amount=500, guild_id=GUILD, timeout_seconds=600)
    assert op.claim_id is not None
    fake_vc.approve(op.claim_id)
    await deliver(db, fake_vc, op.claim_id)
    await worker.webhooks_tick()
    async with db.connect() as conn:
        assert await pool.balance(conn, GUILD, "nyan") == 500
        final = await operations.get(conn, op.id)
    assert final is not None and final.status == "done"


async def test_send_delivers_the_message_to_an_opted_in_receiver(db: AsyncEngine, vc_client: VCClient, fake_vc: FakeVC, worker: Worker, box: SecretBox, fake_discord: FakeDiscord) -> None:
    receiver = 4_000_000_000_000_000_004
    async with db.begin() as conn:
        await users.set_receive_dm(conn, receiver, True)
    op = await start_operation(
        db,
        vc_client,
        kind="send",
        requester_id=REQUESTER,
        unit="nyan",
        recipients=[(receiver, 250)],
        details={"receiver_id": str(receiver), "message": "ありがとう"},
        timeout_seconds=600,
        interaction=interaction(box),
    )
    assert op.claim_id is not None
    fake_vc.approve(op.claim_id)
    await deliver(db, fake_vc, op.claim_id)
    await run_all(worker)
    assert fake_vc.balance(receiver, "nyan") == 250
    [(user, message)] = fake_discord.dms
    assert user == receiver and message["embeds"][0]["fields"][2]["value"] == "ありがとう"


async def test_an_expired_interaction_falls_back_to_a_dm_for_the_result(db: AsyncEngine, vc_client: VCClient, fake_vc: FakeVC, worker: Worker, box: SecretBox, fake_discord: FakeDiscord) -> None:
    fake_discord.fail_edits = True
    op = await start_rain(db, vc_client, box, members=MEMBERS[:1])
    assert op.claim_id is not None
    fake_vc.approve(op.claim_id)
    await deliver(db, fake_vc, op.claim_id)
    await run_all(worker)
    assert [user for user, _ in fake_discord.dms] == [REQUESTER]
    async with db.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(discord_messages).where(discord_messages.c.status == "pending")) == 0
