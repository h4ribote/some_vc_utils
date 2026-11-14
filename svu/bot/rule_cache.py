"""Which guilds have which reward rules, so message and join events in guilds without a rule never touch the database."""

from __future__ import annotations

import time

from sqlalchemy.ext.asyncio import AsyncEngine

from svu.core import guilds

# Rules changed from the dashboard reach the bot within this many seconds; changes made by this bot's commands apply at once.
REFRESH_SECONDS = 30.0


class RuleCache:
    def __init__(self) -> None:
        self._rules: dict[int, frozenset[str]] = {}
        self._loaded_at = float("-inf")

    async def refresh_if_stale(self, engine: AsyncEngine) -> None:
        if time.monotonic() - self._loaded_at < REFRESH_SECONDS:
            return
        async with engine.connect() as conn:
            self._rules = await guilds.guilds_with_rules(conn)
        self._loaded_at = time.monotonic()

    def has(self, guild_id: int, reward_type: str) -> bool:
        return reward_type in self._rules.get(guild_id, frozenset())

    def set(self, guild_id: int, reward_type: str, present: bool) -> None:
        types = set(self._rules.get(guild_id, frozenset()))
        if present:
            types.add(reward_type)
        else:
            types.discard(reward_type)
        self._rules[guild_id] = frozenset(types)
