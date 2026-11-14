"""One-time import of the previous SQLite database (reward_pools, reward_configs, user_reward_cooldowns, receive_msg) into an empty PostgreSQL schema."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from svu.core import pool
from svu.core.guilds import MAX_COOLDOWN_SECONDS
from svu.db.tables import REWARD_TYPES, guild_settings, reward_cooldowns, reward_pool_balances, reward_rules, users

LEGACY_TABLES = ("reward_pools", "reward_configs", "user_reward_cooldowns", "receive_msg")


class SQLiteImportError(Exception):
    """The source is not the previous schema, or the target already holds data."""


@dataclass(frozen=True)
class ImportReport:
    guilds: int
    balances: int
    rules: int
    skipped_rules: int
    cooldowns: int
    users: int


def _read(path: Path) -> dict[str, list[sqlite3.Row]]:
    if not path.is_file():
        raise SQLiteImportError(f"no such file: {path}")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        missing = [t for t in LEGACY_TABLES if t not in present]
        if missing:
            raise SQLiteImportError(f"not the previous Some VC Utils database; missing tables: {', '.join(missing)}")
        return {table: list(conn.execute(f"SELECT * FROM {table}")) for table in LEGACY_TABLES}  # noqa: S608 - table names come from LEGACY_TABLES.
    finally:
        conn.close()


async def import_sqlite(engine: AsyncEngine, path: Path) -> ImportReport:
    """Copy the previous database into the new schema in one transaction; refuses when the target already has guild settings, rules, balances or users."""
    data = _read(path)
    async with engine.begin() as conn:
        for table in (guild_settings, reward_rules, reward_pool_balances, users):
            if await conn.scalar(select(func.count()).select_from(table)):
                raise SQLiteImportError(f"the target database already has rows in {table.name}; import into an empty database")
        balances = 0
        for row in data["reward_pools"]:
            await conn.execute(insert(guild_settings).values(guild_id=int(row["guild_id"]), reward_unit=str(row["unit"]), funding_source="wallet"))
            if int(row["pool_balance"]) > 0:
                await pool.credit(conn, int(row["guild_id"]), str(row["unit"]), int(row["pool_balance"]), "import")
                balances += 1
        rules = skipped = 0
        for row in data["reward_configs"]:
            reward_type = str(row["reward_type"]).lower()
            amount = int(row["amount"])
            cooldown = int(row["cooldown_seconds"] or 0)
            if reward_type not in REWARD_TYPES or amount <= 0 or not 0 <= cooldown <= MAX_COOLDOWN_SECONDS:
                skipped += 1
                continue
            await conn.execute(insert(reward_rules).values(guild_id=int(row["guild_id"]), reward_type=reward_type, amount=amount, cooldown_seconds=cooldown))
            rules += 1
        cooldowns = [
            {
                "guild_id": int(row["guild_id"]),
                "user_id": int(row["user_id"]),
                "reward_type": str(row["reward_type"]).lower(),
                "last_rewarded_at": datetime.fromtimestamp(int(row["last_triggered_timestamp"]), UTC),
            }
            for row in data["user_reward_cooldowns"]
        ]
        if cooldowns:
            await conn.execute(insert(reward_cooldowns), cooldowns)
        receivers = [{"discord_id": int(row["user_id"]), "receive_dm": True} for row in data["receive_msg"]]
        if receivers:
            await conn.execute(insert(users), receivers)
    return ImportReport(guilds=len(data["reward_pools"]), balances=balances, rules=rules, skipped_rules=skipped, cooldowns=len(cooldowns), users=len(receivers))
