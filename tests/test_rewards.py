import asyncio
import threading
from types import SimpleNamespace

import pytest

import bot_commands as cmds
from conftest import FakeVC, member, query
from db_utils import Database

GUILD = SimpleNamespace(id=900, name="guild")


def seed(path, balance=1000, amount=100, cooldown=60, unit="TST", reward_type="message"):
    with Database(path) as db:
        db.init_reward_pool(GUILD.id, unit)
        db.deposit_to_reward_pool(GUILD.id, balance)
        db.set_reward_config(GUILD.id, reward_type, amount, cooldown)


def pool_balance(path):
    return query(path, "SELECT pool_balance FROM reward_pools WHERE guild_id = ?", (GUILD.id,))[0][0]


def cooldowns(path):
    return query(path, "SELECT user_id, guild_id, reward_type, last_triggered_timestamp FROM user_reward_cooldowns ORDER BY user_id")


@pytest.fixture
def vc():
    fake = FakeVC()
    cmds.configure(fake, [1])
    yield fake
    cmds.configure(None, [])


def run(coro):
    return asyncio.run(coro)


def test_normal_payment(db_path, vc):
    seed(db_path)
    run(cmds.handle_reward("message", member(10), GUILD))
    assert vc.payments == [("TST", 10, 100)]
    assert pool_balance(db_path) == 900
    assert len(cooldowns(db_path)) == 1


def test_bot_user_and_missing_rule_ignored(db_path, vc):
    seed(db_path)
    run(cmds.handle_reward("message", member(10, bot=True), GUILD))
    run(cmds.handle_reward("server_join", member(10), GUILD))
    assert vc.attempts == []
    assert pool_balance(db_path) == 1000


def test_insufficient_pool(db_path, vc):
    seed(db_path, balance=50)
    run(cmds.handle_reward("message", member(10), GUILD))
    assert vc.attempts == []
    assert pool_balance(db_path) == 50
    assert cooldowns(db_path) == []


def test_cooldown_blocks_second_reward(db_path, vc):
    seed(db_path)
    run(cmds.handle_reward("message", member(10), GUILD))
    run(cmds.handle_reward("message", member(10), GUILD))
    assert len(vc.payments) == 1
    assert pool_balance(db_path) == 900


def test_concurrent_duplicate_events_pay_once(db_path, vc):
    seed(db_path, cooldown=60)
    vc.delay = 0.05

    async def burst():
        await asyncio.gather(*(cmds.handle_reward("message", member(10), GUILD) for _ in range(10)))

    run(burst())
    assert vc.payments == [("TST", 10, 100)]
    assert pool_balance(db_path) == 900


def test_concurrent_events_never_overdraw_pool(db_path, vc):
    seed(db_path, balance=250, cooldown=0)
    vc.delay = 0.05
    seen = []
    vc.on_pay = lambda *_: seen.append(pool_balance(db_path))

    async def burst():
        await asyncio.gather(*(cmds.handle_reward("message", member(uid), GUILD) for uid in range(10, 20)))

    run(burst())
    assert len(vc.payments) == 2
    assert pool_balance(db_path) == 50
    assert min(seen) >= 0


def test_reservation_is_atomic_across_connections(db_path):
    seed(db_path, balance=10_000, cooldown=60)
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        with Database(db_path) as db:
            results.append(db.reserve_reward(GUILD.id, 10, "message", 1_700_000_000))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is not None for r in results) == 1
    assert pool_balance(db_path) == 9_900


def test_payment_failure_restores_pool_and_removes_new_cooldown(db_path, vc, caplog):
    seed(db_path)
    vc.fail_ids = {10}
    run(cmds.handle_reward("message", member(10), GUILD))
    assert vc.payments == []
    assert pool_balance(db_path) == 1000
    assert cooldowns(db_path) == []
    assert "restoring the pool and cooldown" in caplog.text


def test_payment_failure_restores_previous_cooldown(db_path, vc):
    seed(db_path, cooldown=60)
    with Database(db_path) as db:
        db.update_user_reward_cooldown(10, GUILD.id, "message", 1_000)
    vc.fail_ids = {10}
    run(cmds.handle_reward("message", member(10), GUILD))
    assert pool_balance(db_path) == 1000
    assert cooldowns(db_path) == [(10, GUILD.id, "message", 1_000)]
    vc.fail_ids = set()
    run(cmds.handle_reward("message", member(10), GUILD))
    assert vc.payments == [("TST", 10, 100)]


def test_release_keeps_later_cooldown_and_skips_changed_unit(db_path):
    seed(db_path, cooldown=0)
    with Database(db_path) as db:
        reservation = db.reserve_reward(GUILD.id, 10, "message", 5_000)
        db.update_user_reward_cooldown(10, GUILD.id, "message", 6_000)
        db.conn.commit()
        db.update_reward_pool_unit(GUILD.id, "NEW")
        db.conn.commit()
        assert db.release_reward(reservation) == (False, False)
    assert pool_balance(db_path) == 0
    assert cooldowns(db_path) == [(10, GUILD.id, "message", 6_000)]


def test_unit_change_refund_and_revert(db_path, vc):
    seed(db_path, balance=700, unit="OLD")
    with Database(db_path) as db:
        change = db.change_reward_pool_unit(GUILD.id, "NEW")
    assert (change.old_unit, change.old_balance, change.new_unit) == ("OLD", 700, "NEW")
    assert query(db_path, "SELECT unit, pool_balance FROM reward_pools")[0] == ("NEW", 0)
    with Database(db_path) as db:
        assert db.reserve_reward(GUILD.id, 10, "message", 1) is None
        assert db.revert_reward_pool_unit(change) is True
    assert query(db_path, "SELECT unit, pool_balance FROM reward_pools")[0] == ("OLD", 700)
    with Database(db_path) as db:
        assert db.change_reward_pool_unit(GUILD.id, "OLD") is None
        assert db.deposit_to_reward_pool_if_unit(GUILD.id, "OLD", 5) == (True, "OLD")
        assert db.deposit_to_reward_pool_if_unit(GUILD.id, "OTHER", 5) == (False, "OLD")
    assert pool_balance(db_path) == 705
