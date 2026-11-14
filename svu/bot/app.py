"""The bot role: the Discord gateway connection, slash commands, the guild directory and reward events."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

import discord
from discord import app_commands

from svu import render
from svu.bot.commands import general, payments, reward_pool
from svu.bot.context import BotContext, now
from svu.bot.rule_cache import RuleCache
from svu.config import ConfigError, Settings
from svu.core import guilds, rewards
from svu.crypto import SecretBox
from svu.db.engine import create_engine
from svu.signals import stop_signals
from svu.vc import VCClient

log = logging.getLogger("svu.bot")

CLOSE_TIMEOUT_SECONDS = 15.0

ALL_COMMANDS = [*general.COMMANDS, *payments.COMMANDS, *reward_pool.COMMANDS]


def intents() -> discord.Intents:
    """Guilds for the command context and members for role membership and join events; message content is not needed to reward a message."""
    value = discord.Intents.none()
    value.guilds = True
    value.members = True
    value.guild_messages = True
    return value


class SVUCommandTree(app_commands.CommandTree[Any]):
    async def on_error(self, interaction: discord.Interaction[Any], error: app_commands.AppCommandError) -> None:
        """Answer the user with a short reason; unexpected errors are logged with their traceback."""
        if isinstance(error, app_commands.NoPrivateMessage):
            text = "このコマンドはサーバー内でのみ使えます。"
        elif isinstance(error, app_commands.CheckFailure):
            text = "このコマンドを使う権限がありません。"
        else:
            log.error("command failed", exc_info=error, extra={"command": interaction.command.qualified_name if interaction.command else None})
            text = "内部エラーが発生しました。時間をおいてもう一度お試しください。"
        embed = render.error_embed(text)
        if interaction.response.is_done():
            await interaction.edit_original_response(content=None, embed=embed)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)


class SVUBot(discord.Client):
    def __init__(self, ctx: BotContext) -> None:
        super().__init__(intents=intents(), allowed_mentions=discord.AllowedMentions.none())
        self.ctx = ctx
        self.tree = SVUCommandTree(self)
        for command in ALL_COMMANDS:
            self.tree.add_command(command)

    async def setup_hook(self) -> None:
        await self.tree.sync()
        log.info("slash commands synced", extra={"commands": [c.name for c in self.tree.get_commands()]})

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("connected to Discord", extra={"user_id": self.user.id, "guilds": len(self.guilds)})
        await self.change_presence(activity=discord.Game(name="Some VC Utils"))
        async with self.ctx.engine.begin() as conn:
            await guilds.sync_bot_guilds(conn, _directory(self.guilds))

    async def on_guild_join(self, guild: discord.Guild) -> None:
        async with self.ctx.engine.begin() as conn:
            await guilds.upsert_bot_guild(conn, *_directory([guild]))

    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        async with self.ctx.engine.begin() as conn:
            await guilds.upsert_bot_guild(conn, *_directory([after]))

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        async with self.ctx.engine.begin() as conn:
            await guilds.remove_bot_guild(conn, guild.id)

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is not None and not message.author.bot and message.type in (discord.MessageType.default, discord.MessageType.reply):
            await self.reward(message.guild.id, message.author.id, "message")

    async def on_member_join(self, member: discord.Member) -> None:
        if not member.bot:
            await self.reward(member.guild.id, member.id, "server_join")

    async def reward(self, guild_id: int, user_id: int, reward_type: str) -> None:
        """Queue a reward when a rule applies; the worker pays it."""
        try:
            await self.ctx.rules.refresh_if_stale(self.ctx.engine)
            if not self.ctx.rules.has(guild_id, reward_type):
                return
            async with self.ctx.engine.begin() as conn:
                reservation = await rewards.reserve(conn, guild_id, user_id, reward_type, now())
        except Exception:
            log.exception("reward reservation failed", extra={"guild_id": guild_id, "user_id": user_id, "reward_type": reward_type})
            return
        if reservation is not None:
            log.info("reward queued", extra={"guild_id": guild_id, "user_id": user_id, "reward_type": reward_type, "amount": reservation.amount, "unit": reservation.unit})


def _directory(items: Iterable[discord.Guild]) -> list[guilds.BotGuild]:
    return [guilds.BotGuild(guild_id=g.id, name=g.name, icon=g.icon.key if g.icon else None) for g in items]


def build_bot(settings: Settings) -> SVUBot:
    settings.require("discord_token", "vc_client_id", "vc_client_secret", "secret_key")
    ctx = BotContext(
        settings=settings,
        engine=create_engine(settings.database_url),
        vc=VCClient(settings.vc_base_url, settings.secret("vc_client_id"), settings.secret("vc_client_secret")),
        box=SecretBox(settings.secret("secret_key")),
        rules=RuleCache(),
    )
    return SVUBot(ctx)


async def _run(settings: Settings) -> int:
    bot = build_bot(settings)
    stop = asyncio.Event()
    try:
        with stop_signals(stop.set):
            client_task = asyncio.create_task(bot.start(settings.secret("discord_token")), name="discord")
            stop_task = asyncio.create_task(stop.wait(), name="stop")
            await asyncio.wait({client_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            exit_code = 0
            if client_task.done() and (exc := client_task.exception()) is not None:
                if isinstance(exc, discord.LoginFailure | discord.PrivilegedIntentsRequired):
                    raise ConfigError(f"Discord rejected the bot configuration: {type(exc).__name__}") from exc
                log.error("Discord client failed", exc_info=exc)
                exit_code = 1
            elif client_task.done():
                log.error("Discord client stopped without a stop request")
                exit_code = 1
            stop_task.cancel()
    finally:
        if not bot.is_closed():
            try:
                await asyncio.wait_for(bot.close(), CLOSE_TIMEOUT_SECONDS)
            except Exception:
                log.exception("closing the Discord client failed")
        await bot.ctx.vc.close()
        await bot.ctx.engine.dispose()
    log.info("bot stopped")
    return exit_code


def run_bot(settings: Settings) -> int:
    return asyncio.run(_run(settings))
