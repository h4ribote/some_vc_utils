"""What the bot's commands and events share: settings, the database, the VirtualCrypto client and the credential box."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

import discord
from sqlalchemy.ext.asyncio import AsyncEngine

from svu.bot.rule_cache import RuleCache
from svu.config import Settings
from svu.core.operations import InteractionRef
from svu.crypto import SecretBox
from svu.vc import VCClient

# Discord accepts edits of an interaction's original response for this long after the interaction.
INTERACTION_TOKEN_LIFETIME = timedelta(minutes=15)


@dataclass(frozen=True)
class BotContext:
    settings: Settings
    engine: AsyncEngine
    vc: VCClient
    box: SecretBox
    rules: RuleCache

    def interaction_ref(self, interaction: discord.Interaction[Any]) -> InteractionRef:
        """The reference the worker uses to keep editing this interaction's original response."""
        return InteractionRef(
            application_id=interaction.application_id,
            token_enc=self.box.encrypt(interaction.token),
            expires_at=interaction.created_at.astimezone(UTC) + INTERACTION_TOKEN_LIFETIME,
        )


class _HasContext(Protocol):
    ctx: BotContext


def context_of(interaction: discord.Interaction[Any]) -> BotContext:
    return cast(_HasContext, interaction.client).ctx


def now() -> datetime:
    return datetime.now(UTC)
