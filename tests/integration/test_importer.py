from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from svu.core import guilds, pool, users
from svu.db.tables import reward_cooldowns
from svu.importer import SQLiteImportError, import_sqlite

# The previous bot's schema, as its db_utils.create_tables wrote it.
LEGACY_SCHEMA = """
CREATE TABLE reward_pools (guild_id INTEGER PRIMARY KEY, unit TEXT NOT NULL, pool_balance INTEGER NOT NULL DEFAULT 0);
CREATE TABLE reward_configs (config_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, reward_type TEXT NOT NULL, amount INTEGER NOT NULL, cooldown_seconds INTEGER DEFAULT 0, UNIQUE (guild_id, reward_type));
CREATE TABLE user_reward_cooldowns (user_id INTEGER NOT NULL, guild_id INTEGER NOT NULL, reward_type TEXT NOT NULL, last_triggered_timestamp INTEGER NOT NULL, PRIMARY KEY (user_id, guild_id, reward_type));
CREATE TABLE receive_msg (user_id INTEGER PRIMARY KEY);
"""


def legacy_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute("INSERT INTO reward_pools VALUES (11, 'nyan', 500), (12, 'wan', 0)")
    conn.execute("INSERT INTO reward_configs (guild_id, reward_type, amount, cooldown_seconds) VALUES (11, 'message', 5, 60), (11, 'reaction', 1, 0), (12, 'server_join', 0, 0)")
    conn.execute("INSERT INTO user_reward_cooldowns VALUES (21, 11, 'message', 1700000000)")
    conn.execute("INSERT INTO receive_msg VALUES (21), (22)")
    conn.commit()
    conn.close()
    return path


async def test_import_copies_the_previous_database(db: AsyncEngine, tmp_path: Path) -> None:
    report = await import_sqlite(db, legacy_db(tmp_path / "database.db"))
    assert (report.guilds, report.balances, report.rules, report.skipped_rules, report.cooldowns, report.users) == (2, 1, 1, 2, 1, 2)
    async with db.connect() as conn:
        assert (await guilds.settings(conn, 11)).reward_unit == "nyan"
        assert await pool.balance(conn, 11, "nyan") == 500
        assert [e.kind for e in await pool.entries(conn, 11)] == ["import"]
        assert [(r.reward_type, r.amount, r.cooldown_seconds) for r in await guilds.rules(conn, 11)] == [("message", 5, 60)]
        assert await users.receives_dm(conn, 22)
        stamp = await conn.scalar(select(reward_cooldowns.c.last_rewarded_at))
        assert stamp is not None and int(stamp.timestamp()) == 1700000000


async def test_import_refuses_a_database_that_already_has_data(db: AsyncEngine, tmp_path: Path) -> None:
    source = legacy_db(tmp_path / "database.db")
    await import_sqlite(db, source)
    with pytest.raises(SQLiteImportError, match="already has rows"):
        await import_sqlite(db, source)


async def test_import_refuses_other_files(db: AsyncEngine, tmp_path: Path) -> None:
    other = tmp_path / "other.db"
    sqlite3.connect(other).close()
    with pytest.raises(SQLiteImportError, match="missing tables"):
        await import_sqlite(db, other)
