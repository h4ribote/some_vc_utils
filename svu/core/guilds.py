"""Per-guild settings, reward rules and the directory of guilds the bot is in."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from svu.db.tables import REWARD_TYPES, bot_guilds, guild_settings, reward_rules

MAX_COOLDOWN_SECONDS = 31536000


@dataclass(frozen=True)
class GuildSettings:
    guild_id: int
    reward_unit: str | None
    funding_source: str


@dataclass(frozen=True)
class RewardRule:
    reward_type: str
    amount: int
    cooldown_seconds: int


@dataclass(frozen=True)
class BotGuild:
    guild_id: int
    name: str
    icon: str | None


async def settings(conn: AsyncConnection, guild_id: int) -> GuildSettings:
    row = (await conn.execute(select(guild_settings).where(guild_settings.c.guild_id == guild_id))).first()
    if row is None:
        return GuildSettings(guild_id=guild_id, reward_unit=None, funding_source="wallet")
    return GuildSettings(guild_id=guild_id, reward_unit=row.reward_unit, funding_source=row.funding_source)


async def set_reward_unit(conn: AsyncConnection, guild_id: int, unit: str) -> None:
    stmt = pg_insert(guild_settings).values(guild_id=guild_id, reward_unit=unit)
    await conn.execute(stmt.on_conflict_do_update(index_elements=["guild_id"], set_={"reward_unit": unit, "updated_at": func.now()}))


async def set_funding_source(conn: AsyncConnection, guild_id: int, source: str, *, reward_unit: str | None = None) -> None:
    """Switch where rewards are paid from; reward_unit, when given, replaces the reward unit in the same write."""
    values: dict[str, object] = {"funding_source": source, "updated_at": func.now()}
    if reward_unit is not None:
        values["reward_unit"] = reward_unit
    stmt = pg_insert(guild_settings).values(guild_id=guild_id, funding_source=source, reward_unit=reward_unit)
    await conn.execute(stmt.on_conflict_do_update(index_elements=["guild_id"], set_=values))


def validate_rule(reward_type: str, amount: int, cooldown_seconds: int) -> None:
    if reward_type not in REWARD_TYPES:
        raise ValueError(f"reward_type must be one of {', '.join(REWARD_TYPES)}")
    if amount <= 0:
        raise ValueError("amount must be positive")
    if not 0 <= cooldown_seconds <= MAX_COOLDOWN_SECONDS:
        raise ValueError(f"cooldown_seconds must be between 0 and {MAX_COOLDOWN_SECONDS}")


async def set_rule(conn: AsyncConnection, guild_id: int, reward_type: str, amount: int, cooldown_seconds: int) -> None:
    validate_rule(reward_type, amount, cooldown_seconds)
    stmt = pg_insert(reward_rules).values(guild_id=guild_id, reward_type=reward_type, amount=amount, cooldown_seconds=cooldown_seconds)
    await conn.execute(stmt.on_conflict_do_update(index_elements=["guild_id", "reward_type"], set_={"amount": amount, "cooldown_seconds": cooldown_seconds, "updated_at": func.now()}))


async def delete_rule(conn: AsyncConnection, guild_id: int, reward_type: str) -> bool:
    result = await conn.execute(delete(reward_rules).where(and_(reward_rules.c.guild_id == guild_id, reward_rules.c.reward_type == reward_type)))
    return result.rowcount == 1


async def rules(conn: AsyncConnection, guild_id: int) -> list[RewardRule]:
    rows = await conn.execute(select(reward_rules).where(reward_rules.c.guild_id == guild_id).order_by(reward_rules.c.reward_type))
    return [RewardRule(reward_type=r.reward_type, amount=r.amount, cooldown_seconds=r.cooldown_seconds) for r in rows]


async def guilds_with_rules(conn: AsyncConnection) -> dict[int, frozenset[str]]:
    """Every guild that has at least one reward rule, with its rule types."""
    found: dict[int, set[str]] = {}
    for guild_id, reward_type in await conn.execute(select(reward_rules.c.guild_id, reward_rules.c.reward_type)):
        found.setdefault(guild_id, set()).add(reward_type)
    return {g: frozenset(t) for g, t in found.items()}


async def sync_bot_guilds(conn: AsyncConnection, guilds: Iterable[BotGuild]) -> None:
    """Record the full set of guilds the bot is in after it connects; every other guild is marked absent."""
    present = list(guilds)
    for guild in present:
        await upsert_bot_guild(conn, guild)
    ids = [g.guild_id for g in present]
    await conn.execute(update(bot_guilds).where(bot_guilds.c.guild_id.notin_(ids) if ids else bot_guilds.c.present.is_(True)).values(present=False, updated_at=func.now()))


async def upsert_bot_guild(conn: AsyncConnection, guild: BotGuild) -> None:
    stmt = pg_insert(bot_guilds).values(guild_id=guild.guild_id, name=guild.name, icon=guild.icon, present=True)
    await conn.execute(stmt.on_conflict_do_update(index_elements=["guild_id"], set_={"name": guild.name, "icon": guild.icon, "present": True, "updated_at": func.now()}))


async def remove_bot_guild(conn: AsyncConnection, guild_id: int) -> None:
    await conn.execute(update(bot_guilds).where(bot_guilds.c.guild_id == guild_id).values(present=False, updated_at=func.now()))


async def bot_guild_map(conn: AsyncConnection) -> dict[int, BotGuild]:
    rows = await conn.execute(select(bot_guilds).where(bot_guilds.c.present.is_(True)))
    return {r.guild_id: BotGuild(guild_id=r.guild_id, name=r.name, icon=r.icon) for r in rows}
