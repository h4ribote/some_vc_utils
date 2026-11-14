"""One-time import of the previous SQLite database (reward_pools, reward_configs, user_reward_cooldowns, receive_msg) into an empty PostgreSQL schema."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from svu.core import pool
from svu.core.guilds import MAX_COOLDOWN_SECONDS
from svu.db.tables import REWARD_TYPES, guild_settings, reward_cooldowns, reward_pool_balances, reward_rules, users

LEGACY_TABLES = ("reward_pools", "reward_configs", "user_reward_cooldowns", "receive_msg")


class SQLiteImportError(Exception):
    """The source is not the previous schema, the target already holds data, or the wallet cannot be matched to the pools."""


@dataclass(frozen=True)
class WalletAdjustment:
    """A pool raised from its recorded balance to match what the bot's wallet actually holds in its unit."""

    guild_id: int
    unit: str
    recorded: int
    imported: int


@dataclass(frozen=True)
class ImportReport:
    guilds: int
    balances: int
    rules: int
    skipped_rules: int
    cooldowns: int
    users: int
    adjustments: list[WalletAdjustment] = field(default_factory=list)
    # Units the wallet holds that no pool uses; they stay in the wallet and belong to no guild.
    unassigned: dict[str, int] = field(default_factory=dict)


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


def match_wallet(pools: Mapping[int, tuple[str, int]], wallet: Mapping[str, int]) -> tuple[dict[int, int], list[WalletAdjustment], dict[str, int]]:
    """Pool balances raised so that each unit's pools sum to the wallet's balance in it.

    The surplus of a unit goes to the one guild whose pool uses it; a unit shared by several pools, or a wallet holding less than the pools record, cannot be matched and raises SQLiteImportError.
    """
    balances = {guild: amount for guild, (_, amount) in pools.items()}
    by_unit: dict[str, list[int]] = {}
    for guild, (unit, _) in pools.items():
        by_unit.setdefault(unit, []).append(guild)
    adjustments: list[WalletAdjustment] = []
    for unit, guilds in sorted(by_unit.items()):
        recorded = sum(balances[g] for g in guilds)
        held = wallet.get(unit, 0)
        if held == recorded:
            continue
        if held < recorded:
            raise SQLiteImportError(f"the wallet holds {held} {unit} but the pools record {recorded}")
        if len(guilds) != 1:
            raise SQLiteImportError(f"the wallet holds {held - recorded} {unit} more than the pools record, and {len(guilds)} guilds use {unit}")
        guild = guilds[0]
        balances[guild] = held - recorded + balances[guild]
        adjustments.append(WalletAdjustment(guild_id=guild, unit=unit, recorded=recorded, imported=balances[guild]))
    unassigned = {unit: amount for unit, amount in sorted(wallet.items()) if unit not in by_unit and amount > 0}
    return balances, adjustments, unassigned


async def import_sqlite(engine: AsyncEngine, path: Path, *, wallet: Mapping[str, int] | None = None, dry_run: bool = False) -> ImportReport:
    """Copy the previous database into the new schema in one transaction; refuses when the target already has guild settings, rules, balances or users.

    With wallet (the bot's balances per unit), pool balances are raised to what the wallet holds; with dry_run the transaction is rolled back after the report is built.
    """
    data = _read(path)
    pools = {int(row["guild_id"]): (str(row["unit"]), int(row["pool_balance"])) for row in data["reward_pools"]}
    adjustments: list[WalletAdjustment] = []
    unassigned: dict[str, int] = {}
    balances = {guild: amount for guild, (_, amount) in pools.items()}
    if wallet is not None:
        balances, adjustments, unassigned = match_wallet(pools, wallet)
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            for table in (guild_settings, reward_rules, reward_pool_balances, users):
                if await conn.scalar(select(func.count()).select_from(table)):
                    raise SQLiteImportError(f"the target database already has rows in {table.name}; import into an empty database")
            for guild, (unit, _) in pools.items():
                await conn.execute(insert(guild_settings).values(guild_id=guild, reward_unit=unit, funding_source="wallet"))
                if balances[guild] > 0:
                    await pool.credit(conn, guild, unit, balances[guild], "import")
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
        except BaseException:
            await transaction.rollback()
            raise
        if dry_run:
            await transaction.rollback()
        else:
            await transaction.commit()
    return ImportReport(
        guilds=len(pools),
        balances=sum(1 for amount in balances.values() if amount > 0),
        rules=rules,
        skipped_rules=skipped,
        cooldowns=len(cooldowns),
        users=len(receivers),
        adjustments=adjustments,
        unassigned=unassigned,
    )
