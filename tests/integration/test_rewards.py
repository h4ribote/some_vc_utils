"""Reward reservation, its compensation, wallet and issue funding, and the guild issuing grant lifecycle."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from svu import guild_admin
from svu.core import grants, guilds, payouts, pool, rewards, webhooks
from svu.crypto import SecretBox
from svu.db.tables import reward_cooldowns
from svu.vc import VCClient
from svu.worker.jobs import Worker
from tests.fake_vc import APP_DISCORD_ID, FakeVC
from tests.integration.conftest import GUILD, Clock

USER = 5_000_000_000_000_000_005
ADMIN = 6_000_000_000_000_000_006
NOW = datetime(2026, 6, 1, tzinfo=UTC)


async def wallet_guild(db: AsyncEngine, balance: int, amount: int = 10, cooldown: int = 60) -> None:
    async with db.begin() as conn:
        await guilds.set_reward_unit(conn, GUILD, "nyan")
        await guilds.set_rule(conn, GUILD, "message", amount, cooldown)
        if balance:
            await pool.credit(conn, GUILD, "nyan", balance, "deposit")


async def cooldown_of(db: AsyncEngine, user: int) -> datetime | None:
    async with db.connect() as conn:
        return await conn.scalar(select(reward_cooldowns.c.last_rewarded_at).where(reward_cooldowns.c.user_id == user))


async def test_cooldown_and_balance_gate_rewards(db: AsyncEngine) -> None:
    await wallet_guild(db, balance=25)
    async with db.begin() as conn:
        assert await rewards.reserve(conn, GUILD, USER, "message", NOW) is not None
        assert await rewards.reserve(conn, GUILD, USER, "message", NOW + timedelta(seconds=30)) is None
        assert await rewards.reserve(conn, GUILD, USER, "message", NOW + timedelta(seconds=61)) is not None
        assert await rewards.reserve(conn, GUILD, USER, "message", NOW + timedelta(seconds=200)) is None, "only 5 left"
        assert await rewards.reserve(conn, GUILD, USER, "server_join", NOW) is None, "no rule"
        assert await pool.balance(conn, GUILD, "nyan") == 5
    assert await cooldown_of(db, USER) == NOW + timedelta(seconds=61)


async def test_concurrent_events_never_overdraw_the_pool(db: AsyncEngine) -> None:
    await wallet_guild(db, balance=50, cooldown=0)

    async def one(user: int) -> bool:
        async with db.begin() as conn:
            return await rewards.reserve(conn, GUILD, user, "message", NOW) is not None

    results = await asyncio.gather(*(one(USER + i % 7) for i in range(30)))
    assert sum(results) == 5
    async with db.connect() as conn:
        assert await pool.balance(conn, GUILD, "nyan") == 0


async def test_concurrent_events_for_one_user_pay_once_per_cooldown(db: AsyncEngine) -> None:
    await wallet_guild(db, balance=1000, cooldown=3600)

    async def one() -> bool:
        async with db.begin() as conn:
            return await rewards.reserve(conn, GUILD, USER, "message", NOW) is not None

    assert sum(await asyncio.gather(*(one() for _ in range(10)))) == 1


async def test_a_refused_reward_returns_to_the_pool_and_restores_the_cooldown(db: AsyncEngine, worker: Worker, fake_vc: FakeVC) -> None:
    await wallet_guild(db, balance=10)
    async with db.begin() as conn:
        reservation = await rewards.reserve(conn, GUILD, USER, "message", NOW)
    assert reservation is not None
    await worker.payouts_tick()
    async with db.connect() as conn:
        payout = await payouts.get(conn, reservation.payout_id)
        assert payout is not None and payout.status == "failed", "the bot's wallet is empty, so VirtualCrypto refuses"
        assert await pool.balance(conn, GUILD, "nyan") == 10
    assert await cooldown_of(db, USER) is None


async def test_a_wallet_reward_is_paid_from_the_bot(db: AsyncEngine, worker: Worker, fake_vc: FakeVC) -> None:
    fake_vc.give(APP_DISCORD_ID, "nyan", 10)
    await wallet_guild(db, balance=10)
    async with db.begin() as conn:
        assert await rewards.reserve(conn, GUILD, USER, "message", NOW) is not None
    await worker.payouts_tick()
    assert fake_vc.balance(USER, "nyan") == 10
    assert fake_vc.balance(APP_DISCORD_ID, "nyan") == 0


async def test_withdraw_pays_the_admin_and_is_refused_beyond_the_balance(db: AsyncEngine, worker: Worker, fake_vc: FakeVC) -> None:
    fake_vc.give(APP_DISCORD_ID, "nyan", 30)
    await wallet_guild(db, balance=30)
    await guild_admin.withdraw(db, GUILD, "nyan", 30, ADMIN)
    with pytest.raises(guild_admin.AdminError):
        await guild_admin.withdraw(db, GUILD, "nyan", 1, ADMIN)
    await worker.payouts_tick()
    assert fake_vc.balance(ADMIN, "nyan") == 30


async def approve_issue_grant(db: AsyncEngine, worker: Worker, vc_client: VCClient, box: SecretBox, fake_vc: FakeVC) -> None:
    request = await guild_admin.use_issue(db, vc_client, box, GUILD, ADMIN)
    assert request.user_code is not None and request.unit == "nyan"
    await worker.grant_requests_tick()
    async with db.connect() as conn:
        assert await grants.active_grant(conn, GUILD) is None
    fake_vc.approve_grant(request.user_code)
    async with db.begin() as conn:
        await webhooks.record(conn, 3, {"type": 3, "data": {"guild_id": str(GUILD), "discord_id": None, "grant_id": "42", "scopes": ["vc.issue"]}})
    await worker.webhooks_tick()
    await worker.grant_requests_tick()


async def test_issue_grant_switches_rewards_to_the_guild_pool(db: AsyncEngine, worker: Worker, vc_client: VCClient, box: SecretBox, fake_vc: FakeVC) -> None:
    await approve_issue_grant(db, worker, vc_client, box, fake_vc)
    async with db.begin() as conn:
        settings = await guilds.settings(conn, GUILD)
        assert (settings.funding_source, settings.reward_unit) == ("issue", "nyan")
        await guilds.set_rule(conn, GUILD, "server_join", 40, 0)
        reservation = await rewards.reserve(conn, GUILD, USER, "server_join", NOW)
    assert reservation is not None and reservation.method == "issue"
    await worker.payouts_tick()
    assert fake_vc.balance(USER, "nyan") == 40
    assert fake_vc.pools[GUILD] == 10_000 - 40
    await worker.messages_tick()


async def test_an_expired_guild_token_is_refreshed_before_issuing(db: AsyncEngine, worker: Worker, vc_client: VCClient, box: SecretBox, fake_vc: FakeVC) -> None:
    await approve_issue_grant(db, worker, vc_client, box, fake_vc)
    fake_vc.expire_guild_tokens()
    async with db.begin() as conn:
        await guilds.set_rule(conn, GUILD, "server_join", 5, 0)
        assert await rewards.reserve(conn, GUILD, USER, "server_join", NOW) is not None
    await worker.payouts_tick()
    assert fake_vc.balance(USER, "nyan") == 5


async def test_tokens_near_expiry_are_refreshed_by_the_job(db: AsyncEngine, worker: Worker, vc_client: VCClient, box: SecretBox, fake_vc: FakeVC) -> None:
    await approve_issue_grant(db, worker, vc_client, box, fake_vc)
    async with db.connect() as conn:
        before = await grants.active_grant(conn, GUILD)
    assert before is not None and isinstance(worker.clock, Clock)
    worker.clock.advance(minutes=55)
    await worker.refresh_grants_tick()
    async with db.connect() as conn:
        after = await grants.active_grant(conn, GUILD)
    assert after is not None and box.decrypt(after.access_token_enc) != box.decrypt(before.access_token_enc)


async def test_a_revoked_grant_fails_the_reward_and_stops_rewards(db: AsyncEngine, worker: Worker, vc_client: VCClient, box: SecretBox, fake_vc: FakeVC) -> None:
    await approve_issue_grant(db, worker, vc_client, box, fake_vc)
    fake_vc.revoke_guild(GUILD)
    async with db.begin() as conn:
        await guilds.set_rule(conn, GUILD, "server_join", 5, 0)
        reservation = await rewards.reserve(conn, GUILD, USER, "server_join", NOW)
    assert reservation is not None
    await worker.payouts_tick()
    async with db.connect() as conn:
        payout = await payouts.get(conn, reservation.payout_id)
        assert payout is not None and payout.status == "failed"
        assert await grants.active_grant(conn, GUILD) is None
    assert await cooldown_of(db, USER) is None
    async with db.begin() as conn:
        assert await rewards.reserve(conn, GUILD, USER, "server_join", NOW) is None


async def test_a_revocation_webhook_revokes_only_the_named_grant(db: AsyncEngine, worker: Worker, vc_client: VCClient, box: SecretBox, fake_vc: FakeVC) -> None:
    await approve_issue_grant(db, worker, vc_client, box, fake_vc)
    async with db.begin() as conn:
        await webhooks.record(conn, 3, {"type": 3, "data": {"guild_id": str(GUILD), "grant_id": "999", "scopes": []}})
    await worker.webhooks_tick()
    async with db.connect() as conn:
        assert await grants.active_grant(conn, GUILD) is not None
    async with db.begin() as conn:
        await webhooks.record(conn, 3, {"type": 3, "data": {"guild_id": str(GUILD), "grant_id": "42", "scopes": []}})
    await worker.webhooks_tick()
    async with db.connect() as conn:
        assert await grants.active_grant(conn, GUILD) is None


async def test_vc_sync_points_the_webhook_here_and_stores_the_key(db: AsyncEngine, worker: Worker, fake_vc: FakeVC) -> None:
    await worker.vc_sync_tick()
    assert fake_vc.app["webhook_url"] == "https://svu.example.com/vc/webhook"
    assert fake_vc.app["subscribed_events"] == [2, 3]
    assert "refresh_token" in fake_vc.app["grant_types"]
    async with db.connect() as conn:
        assert await webhooks.public_key(conn) == fake_vc.public_key_hex
    patches = len([r for r in fake_vc.requests if r.method == "PATCH"])
    await worker.vc_sync_tick()
    assert len([r for r in fake_vc.requests if r.method == "PATCH"]) == patches
