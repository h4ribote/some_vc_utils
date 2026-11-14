"""The command tree as Discord validates it when the bot syncs its commands."""

from __future__ import annotations

from typing import Any

import discord
from discord import app_commands

from svu.bot.app import ALL_COMMANDS

# Discord's limits on a command payload.
DISCORD_INTEGER_MAX = 2**53 - 1
NAME_MAX = 32
DESCRIPTION_MAX = 100


def payloads() -> list[dict[str, Any]]:
    tree: app_commands.CommandTree[Any] = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    return [command.to_dict(tree) for command in ALL_COMMANDS]


def options(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found = []
    for option in payload.get("options", []):
        found.append(option)
        found.extend(options(option))
    return found


def test_integer_options_stay_within_discord_bounds() -> None:
    for payload in payloads():
        for option in options(payload):
            for bound in ("min_value", "max_value"):
                if bound in option:
                    assert abs(option[bound]) <= DISCORD_INTEGER_MAX, f"{payload['name']} {option['name']} {bound}"


def test_names_and_descriptions_fit() -> None:
    for payload in payloads():
        for item in [payload, *options(payload)]:
            assert len(item["name"]) <= NAME_MAX
            assert 1 <= len(item["description"]) <= DESCRIPTION_MAX, item["name"]
