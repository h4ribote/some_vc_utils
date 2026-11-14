"""Fixtures that wire the worker and the services to the fake VirtualCrypto and a recording fake Discord."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from svu.config import Settings
from svu.crypto import SecretBox
from svu.discord_rest import DiscordRest
from svu.vc import VCClient
from svu.worker.jobs import Worker
from tests.fake_vc import APP_DISCORD_ID, CLIENT_ID, CLIENT_SECRET, FakeVC

GUILD = 1_000_000_000_000_000_001
REQUESTER = 2_000_000_000_000_000_002
SECRET_KEY = "s" * 48


@dataclass
class FakeDiscord:
    """Records interaction edits and DMs; fail_edits makes every edit answer 404 Unknown Webhook."""

    edits: list[dict[str, Any]] = field(default_factory=list)
    dms: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    fail_edits: bool = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v10")
        if path.startswith("/webhooks/"):
            if self.fail_edits:
                return httpx.Response(404, json={"message": "Unknown Webhook"})
            self.edits.append(json.loads(request.content))
            return httpx.Response(200, json={})
        if path == "/users/@me/channels":
            return httpx.Response(200, json={"id": json.loads(request.content)["recipient_id"]})
        if path.startswith("/channels/"):
            self.dms.append((int(path.split("/")[2]), json.loads(request.content)))
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})


class Clock:
    """Real time shifted by an offset that tests advance, so rows stamped with the database's now() stay comparable."""

    def __init__(self) -> None:
        self.offset = timedelta()

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset

    def advance(self, **kwargs: float) -> None:
        self.offset += timedelta(**kwargs)


@pytest.fixture
def fake_vc() -> FakeVC:
    vc = FakeVC()
    vc.add_currency("nyan", guild_id=GUILD, pool=10_000)
    vc.give(REQUESTER, "nyan", 100_000)
    vc.give(APP_DISCORD_ID, "nyan", 0)
    return vc


@pytest.fixture
def fake_discord() -> FakeDiscord:
    return FakeDiscord()


@pytest.fixture
def box() -> SecretBox:
    return SecretBox(SECRET_KEY)


@pytest.fixture
def settings(tmp_path: Any) -> Settings:
    return Settings(
        _env_file=None,
        db_password="unused-password",
        public_base_url="https://svu.example.com",
        secret_key=SECRET_KEY,
        backup_dir=tmp_path / "backups",
        admin_ids="42",
    )


@pytest_asyncio.fixture(loop_scope="session")
async def vc_client(fake_vc: FakeVC) -> AsyncIterator[VCClient]:
    client = VCClient("https://vc.test", CLIENT_ID, CLIENT_SECRET, transport=fake_vc.transport())
    yield client
    await client.close()


@pytest_asyncio.fixture(loop_scope="session")
async def worker(db: AsyncEngine, vc_client: VCClient, fake_discord: FakeDiscord, box: SecretBox, settings: Settings) -> AsyncIterator[Worker]:
    discord = DiscordRest("bot-token", transport=httpx.MockTransport(fake_discord.handle))
    clock = Clock()
    yield Worker(settings=settings, engine=db, vc=vc_client, discord=discord, box=box, clock=clock)
    await discord.close()
