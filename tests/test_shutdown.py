import asyncio
import os
import signal
from types import SimpleNamespace

import discord
import pytest

import bot_commands as cmds
import main
from conftest import FakeVC
from settings import EXIT_CONFIG_ERROR, Settings

SETTINGS = Settings(discord_token="token", admin_ids=frozenset({1}), vc_client_id="id", vc_client_secret="secret")


class FakeClient:
    """Mimics discord.Client.start/close: start() blocks until close() is called."""

    instances = []

    def __init__(self, settings, shutdown, start_error=None, on_start=None):
        self.shutdown = shutdown
        self.start_error = start_error
        self.on_start = on_start
        self._closed = asyncio.Event()
        self.close_calls = 0
        self.vc_during_run = None
        FakeClient.instances.append(self)

    async def start(self, token):
        self.vc_during_run = cmds.get_vc_client()
        if self.on_start:
            self.on_start(self)
        if self.start_error:
            raise self.start_error
        await self._closed.wait()

    async def close(self):
        self.close_calls += 1
        self._closed.set()

    def is_closed(self):
        return self._closed.is_set()


@pytest.fixture(autouse=True)
def no_backups(monkeypatch):
    state = {"cancelled": False}

    async def idle_backup_loop(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    monkeypatch.setattr(main, "backup_loop", idle_backup_loop)
    FakeClient.instances.clear()
    return state


def run(factory, vc):
    return asyncio.run(main.run_bot(SETTINGS, client_factory=factory, vc_factory=lambda *_: vc))


def test_sigterm_closes_client_and_exits_zero(no_backups):
    vc = FakeVC()

    def factory(settings, shutdown):
        asyncio.get_running_loop().call_later(0.2, os.kill, os.getpid(), signal.SIGTERM)
        return FakeClient(settings, shutdown)

    assert run(factory, vc) == 0
    client = FakeClient.instances[0]
    assert client.close_calls == 1
    assert client.vc_during_run is vc
    assert vc.started and vc.closed
    assert no_backups["cancelled"]
    with pytest.raises(RuntimeError):
        cmds.get_vc_client()
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL


def test_sigint_also_exits_zero():
    def factory(settings, shutdown):
        asyncio.get_running_loop().call_later(0.1, os.kill, os.getpid(), signal.SIGINT)
        return FakeClient(settings, shutdown)

    assert run(factory, FakeVC()) == 0


def test_kill_request_uses_same_path():
    vc = FakeVC()

    def factory(settings, shutdown):
        return FakeClient(settings, shutdown, on_start=lambda c: asyncio.get_running_loop().call_later(0.1, c.shutdown.request, 0, "kill command"))

    assert run(factory, vc) == 0
    assert FakeClient.instances[0].close_calls == 1 and vc.closed


def test_login_failure_is_config_error():
    vc = FakeVC()
    rc = run(lambda s, sd: FakeClient(s, sd, start_error=discord.LoginFailure("bad token")), vc)
    assert rc == EXIT_CONFIG_ERROR
    assert vc.closed


def test_unexpected_client_error_exits_one():
    assert run(lambda s, sd: FakeClient(s, sd, start_error=RuntimeError("boom")), FakeVC()) == 1


def test_kill_mention_from_admin_requests_shutdown():
    async def scenario(author_id):
        shutdown = main.Shutdown()
        client = main.SomeVCUtilsClient(SETTINGS, shutdown)
        client._connection.user = SimpleNamespace(id=999)
        message = SimpleNamespace(author=SimpleNamespace(id=author_id, bot=False), guild=None, content="<@999> kill")
        await client.on_message(message)
        return shutdown.exit_code

    assert asyncio.run(scenario(1)) == 0
    assert asyncio.run(scenario(2)) is None
