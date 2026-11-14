"""The API: the signed webhook receiver and the dashboard's login, authorization and forms."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from svu.api.app import create_app
from svu.api.dashboard import COOKIE
from svu.api.discord_oauth import DiscordOAuth
from svu.api.state import ApiState
from svu.config import Settings
from svu.core import guilds, sessions, webhooks
from svu.crypto import SecretBox
from svu.db.tables import reward_rules, webhook_events
from svu.vc import VCClient
from tests.fake_vc import FakeVC
from tests.integration.conftest import GUILD

USER = 7_000_000_000_000_000_007
OTHER_GUILD = 8_000_000_000_000_000_008
BASE = "https://svu.example.com"


@dataclass
class FakeDiscordOAuth:
    guild_list: list[dict[str, Any]] = field(default_factory=list)
    user_id: int = USER

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v10")
        if path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "user-token", "token_type": "Bearer"})
        if request.headers.get("Authorization") != "Bearer user-token":
            return httpx.Response(401, json={})
        if path == "/users/@me":
            return httpx.Response(200, json={"id": str(self.user_id), "username": "tester", "global_name": "Tester", "avatar": None})
        if path == "/users/@me/guilds":
            return httpx.Response(200, json=self.guild_list)
        return httpx.Response(404, json={})


@pytest.fixture
def oauth_fake() -> FakeDiscordOAuth:
    return FakeDiscordOAuth(
        guild_list=[
            {"id": str(GUILD), "name": "Managed", "icon": None, "owner": False, "permissions": str(0x20)},
            {"id": str(OTHER_GUILD), "name": "Member only", "icon": None, "owner": False, "permissions": "0"},
        ]
    )


@pytest_asyncio.fixture(loop_scope="session")
async def client(db: AsyncEngine, vc_client: VCClient, box: SecretBox, settings: Settings, oauth_fake: FakeDiscordOAuth) -> AsyncIterator[httpx.AsyncClient]:
    oauth = DiscordOAuth(123, "client-secret", BASE + "/auth/callback", transport=httpx.MockTransport(oauth_fake.handle))
    app = create_app(ApiState(settings=settings, engine=db, vc=vc_client, box=box, oauth=oauth))
    async with db.begin() as conn:
        await guilds.upsert_bot_guild(conn, guilds.BotGuild(guild_id=GUILD, name="Managed", icon=None))
        await guilds.upsert_bot_guild(conn, guilds.BotGuild(guild_id=OTHER_GUILD, name="Member only", icon=None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as http:
        yield http
    await oauth.close()


async def store_key(db: AsyncEngine, fake_vc: FakeVC) -> None:
    async with db.begin() as conn:
        await webhooks.save_app_state(conn, public_key=fake_vc.public_key_hex, webhook_url=None, webhook_verified_at=None, webhook_failed_at=None)


async def post_event(client: httpx.AsyncClient, fake_vc: FakeVC, payload: dict[str, Any], *, signer: FakeVC | None = None) -> httpx.Response:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", **(signer or fake_vc).sign(body)}
    return await client.post("/vc/webhook", content=body, headers=headers)


async def test_webhook_answers_a_signed_ping_and_refuses_a_forged_one(client: httpx.AsyncClient, db: AsyncEngine, fake_vc: FakeVC) -> None:
    await store_key(db, fake_vc)
    ok = await post_event(client, fake_vc, {"type": 1})
    assert ok.status_code == 200 and ok.json() == {"type": 1}
    forged = await post_event(client, fake_vc, {"type": 1}, signer=FakeVC())
    assert forged.status_code == 401


async def test_webhook_refuses_everything_before_the_key_is_known(client: httpx.AsyncClient, fake_vc: FakeVC) -> None:
    assert (await post_event(client, fake_vc, {"type": 1})).status_code == 401


async def test_webhook_stores_events_for_the_worker(client: httpx.AsyncClient, db: AsyncEngine, fake_vc: FakeVC) -> None:
    await store_key(db, fake_vc)
    response = await post_event(client, fake_vc, {"type": 2, "data": []})
    assert response.status_code == 200
    async with db.connect() as conn:
        assert (await conn.execute(select(webhook_events.c.type))).scalars().all() == [2]


async def login(client: httpx.AsyncClient) -> str:
    """Log in through the real flow and return the CSRF token of the new session."""
    start = await client.get("/login", params={"next": f"/guilds/{GUILD}"})
    assert start.status_code == 303
    state = httpx.URL(start.headers["location"]).params["state"]
    done = await client.get("/auth/callback", params={"code": "c", "state": state})
    assert done.status_code == 303 and done.headers["location"] == f"/guilds/{GUILD}"
    assert COOKIE in client.cookies
    page = await client.get("/me")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert token is not None
    return token.group(1)


async def test_pages_need_a_login(client: httpx.AsyncClient) -> None:
    response = await client.get(f"/guilds/{GUILD}")
    assert response.status_code == 303 and response.headers["location"] == f"/login?next=/guilds/{GUILD}"
    assert (await client.get("/")).status_code == 200


async def test_a_state_works_once(client: httpx.AsyncClient) -> None:
    start = await client.get("/login")
    state = httpx.URL(start.headers["location"]).params["state"]
    assert (await client.get("/auth/callback", params={"code": "c", "state": state})).status_code == 303
    assert (await client.get("/auth/callback", params={"code": "c", "state": state})).status_code == 400


async def test_only_manageable_guilds_with_the_bot_are_shown(client: httpx.AsyncClient) -> None:
    await login(client)
    index = await client.get("/")
    assert "Managed" in index.text and "Member only" not in index.text
    assert (await client.get(f"/guilds/{GUILD}")).status_code == 200
    assert (await client.get(f"/guilds/{OTHER_GUILD}")).status_code == 404
    assert (await client.get("/guilds/12345")).status_code == 404


async def test_rule_form_needs_the_session_csrf_token_and_our_origin(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    csrf = await login(client)
    form = {"reward_type": "message", "amount": "15", "cooldown_seconds": "30"}
    assert (await client.post(f"/guilds/{GUILD}/rules", data={**form, "csrf_token": "wrong"})).status_code == 400
    assert (await client.post(f"/guilds/{GUILD}/rules", data={**form, "csrf_token": csrf}, headers={"Origin": "https://evil.example"})).status_code == 400
    ok = await client.post(f"/guilds/{GUILD}/rules", data={**form, "csrf_token": csrf}, headers={"Origin": BASE})
    assert ok.status_code == 303
    async with db.connect() as conn:
        assert (await conn.execute(select(reward_rules.c.amount, reward_rules.c.cooldown_seconds))).all() == [(15, 30)]
    page = await client.get(f"/guilds/{GUILD}")
    assert "保存しました" in page.text


async def test_forms_for_unmanaged_guilds_are_refused(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    csrf = await login(client)
    response = await client.post(f"/guilds/{OTHER_GUILD}/rules", data={"csrf_token": csrf, "reward_type": "message", "amount": "1", "cooldown_seconds": "0"})
    assert response.status_code == 404
    async with db.connect() as conn:
        assert (await conn.execute(select(reward_rules))).all() == []


async def test_admin_page_is_only_for_bot_admins(client: httpx.AsyncClient, db: AsyncEngine, box: SecretBox) -> None:
    await login(client)
    assert (await client.get("/admin")).status_code == 404
    async with db.begin() as conn:
        token = await sessions.create(conn, user_id=42, username="admin", avatar=None, discord_token_enc=box.encrypt("user-token"), now=datetime.now(UTC))
    client.cookies.set(COOKIE, token, domain="svu.example.com")
    assert (await client.get("/admin")).status_code == 200


async def test_security_headers(client: httpx.AsyncClient) -> None:
    response = await client.get("/")
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_health(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health")).json() == {"status": "ok"}
