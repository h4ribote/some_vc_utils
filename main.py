"""Entry point of the Some VC Utils Discord bot; exits 0 on SIGTERM, SIGINT or the admin kill command."""

import asyncio
import logging
import os
import signal
import sys
from typing import Callable, Optional

import discord
from discord import app_commands

import bot_commands as cmds
from backup import backup_loop
from db_utils import init_database
from logging_setup import setup_logging
from settings import EXIT_CONFIG_ERROR, ConfigError, Settings, load_settings

log = logging.getLogger("svu")

CLOSE_TIMEOUT_SECONDS = 15
SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)


class Shutdown:
    """Records the first shutdown request and the exit code that goes with it."""

    def __init__(self):
        self._event = asyncio.Event()
        self.exit_code: Optional[int] = None

    def request(self, exit_code: int, reason: str) -> None:
        if self.exit_code is not None:
            return
        log.info("Shutdown requested (%s)", reason)
        self.exit_code = exit_code
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


@app_commands.command(name="info", description="このボットに関する情報を表示します")
async def info_command(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    await interaction.followup.send(embed=cmds.bot_info())


class SomeVCUtilsClient(discord.Client):
    def __init__(self, settings: Settings, shutdown: Shutdown):
        intents = discord.Intents.default()
        intents.members = True
        intents.presences = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.admin_ids = settings.admin_ids
        self.shutdown = shutdown

    async def setup_hook(self):
        for command in (cmds.rain, cmds.send_with_msg, cmds.receive_msg, cmds.admin_refresh, cmds.reward_pool, info_command):
            self.tree.add_command(command)
        try:
            await self.tree.sync()
            log.info("Slash commands synced")
        except Exception:
            log.exception("Slash command sync failed")

    async def on_ready(self):
        log.info("Logged in as %s (id %s)", self.user, self.user.id)
        await self.change_presence(activity=discord.Game(name="Some VC Utils by h4ribote"), status=discord.Status.online)

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if message.guild:
            await cmds.handle_reward('message', message.author, message.guild)

        if message.content.startswith(f"<@{self.user.id}>") and message.author.id in self.admin_ids:
            parts = message.content.split(' ')
            if len(parts) > 1 and parts[1] == "kill":
                self.shutdown.request(0, f"kill command from user {message.author.id}")

    async def on_member_join(self, member: discord.Member):
        if member.guild:
            await cmds.handle_reward('server_join', member, member.guild)


def _exit_code_for_client_error(exc: BaseException) -> int:
    if isinstance(exc, (discord.LoginFailure, discord.PrivilegedIntentsRequired)):
        log.error("Discord rejected the configuration: %s: %s", type(exc).__name__, exc)
        return EXIT_CONFIG_ERROR
    log.error("Discord client failed", exc_info=exc)
    return 1


async def run_bot(
    settings: Settings,
    client_factory: Callable[[Settings, Shutdown], discord.Client] = SomeVCUtilsClient,
    vc_factory: Callable[[str, str], object] = cmds.new_vc_client,
) -> int:
    """Run until a shutdown request or a client failure, then close Discord and VirtualCrypto and return the exit code."""
    loop = asyncio.get_running_loop()
    shutdown = Shutdown()
    for sig in SHUTDOWN_SIGNALS:
        loop.add_signal_handler(sig, shutdown.request, 0, f"signal {sig.name}")

    vc_client = vc_factory(settings.vc_client_id, settings.vc_client_secret)
    client = None
    tasks: list[asyncio.Task] = []
    exit_code = 1
    try:
        await vc_client.start()
        cmds.configure(vc_client, settings.admin_ids)
        client = client_factory(settings, shutdown)
        backup_task = asyncio.create_task(backup_loop(settings.backup_interval_hours, settings.backup_retention), name="backup")
        client_task = asyncio.create_task(client.start(settings.discord_token), name="discord")
        shutdown_task = asyncio.create_task(shutdown.wait(), name="shutdown")
        tasks = [backup_task, client_task, shutdown_task]

        await asyncio.wait({client_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED)
        if shutdown.exit_code is not None:
            exit_code = shutdown.exit_code
        elif client_task.exception() is not None:
            exit_code = _exit_code_for_client_error(client_task.exception())
        else:
            log.error("Discord client stopped without a shutdown request")
            exit_code = 1
    finally:
        if client is not None and not client.is_closed():
            try:
                await asyncio.wait_for(client.close(), CLOSE_TIMEOUT_SECONDS)
                log.info("Disconnected from Discord")
            except Exception:
                log.exception("Closing the Discord client failed")
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await vc_client.close()
        except Exception:
            log.exception("Closing the VirtualCrypto client failed")
        cmds.configure(None, frozenset())
        for sig in SHUTDOWN_SIGNALS:
            loop.remove_signal_handler(sig)
    return exit_code


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as e:
        print(e.report(), file=sys.stderr)
        return EXIT_CONFIG_ERROR
    setup_logging(settings)
    log.info("Starting Some VC Utils (pid %d)", os.getpid())
    try:
        init_database()
    except Exception:
        log.exception("Opening the database failed")
        return 1
    exit_code = asyncio.run(run_bot(settings))
    log.info("Exiting with code %d", exit_code)
    logging.shutdown()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
