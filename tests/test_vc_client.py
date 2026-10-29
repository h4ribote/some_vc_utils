"""The async VirtualCrypto client against a local aiohttp server standing in for the API (no external network)."""

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from virtualcrypto import async_client
from virtualcrypto.errors import BadRequest, HTTPException


class FakeAPI:
    def __init__(self):
        self.tokens_issued = 0
        self.valid = set()
        self.pay_status = 200
        self.payments = []
        self.app = web.Application()
        self.app.router.add_post("/oauth2/token", self.token)
        self.app.router.add_post("/api/v1/users/@me/transactions", self.pay)

    async def token(self, request):
        if request.headers.get("Authorization", "") != "Basic Y2lkOnNlY3JldA==":
            return web.json_response({"error": "invalid_client"}, status=401)
        await asyncio.sleep(0.02)
        self.tokens_issued += 1
        token = f"tok{self.tokens_issued}"
        self.valid.add(token)
        return web.json_response({"access_token": token, "expires_in": 3600, "token_type": "Bearer"})

    async def pay(self, request):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in self.valid:
            return web.json_response({"error": "invalid_token"}, status=401)
        if self.pay_status != 200:
            return web.json_response({"error_info": "nope"}, status=self.pay_status)
        self.payments.append((token, dict(await request.post())))
        return web.Response(status=200)


async def with_client(api, monkeypatch, scenario, secret="secret"):
    server = TestServer(api.app)
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    monkeypatch.setattr(async_client, "VIRTUALCRYPTO_API", base + "/api/v1")
    monkeypatch.setattr(async_client, "VIRTUALCRYPTO_TOKEN_ENDPOINT", base + "/oauth2/token")
    client = async_client.AsyncVirtualCryptoClient("cid", secret, [async_client.Scope.Pay, async_client.Scope.Claim])
    await client.start()
    try:
        await scenario(client)
    finally:
        await client.close()
        await server.close()


def test_token_fetched_once_and_shared(monkeypatch):
    api = FakeAPI()

    async def scenario(client):
        await asyncio.gather(*(client.pay("TST", 10 + i, 1) for i in range(5)))

    asyncio.run(with_client(api, monkeypatch, scenario))
    assert api.tokens_issued == 1
    assert len(api.payments) == 5


def test_expired_token_is_refreshed(monkeypatch):
    api = FakeAPI()

    async def scenario(client):
        await client.pay("TST", 10, 1)
        client._token_deadline = 0
        await client.pay("TST", 10, 1)

    asyncio.run(with_client(api, monkeypatch, scenario))
    assert api.tokens_issued == 2
    assert [p[0] for p in api.payments] == ["tok1", "tok2"]


def test_rejected_token_is_renewed_and_request_retried(monkeypatch):
    api = FakeAPI()

    async def scenario(client):
        await client.pay("TST", 10, 1)
        api.valid.clear()
        await client.pay("TST", 11, 2)

    asyncio.run(with_client(api, monkeypatch, scenario))
    assert api.tokens_issued == 2
    assert api.payments[-1] == ("tok2", {"unit": "TST", "receiver_discord_id": "11", "amount": "2"})


def test_non_success_payment_raises(monkeypatch):
    api = FakeAPI()

    async def scenario(client):
        api.pay_status = 500
        with pytest.raises(HTTPException) as info:
            await client.pay("TST", 10, 1)
        assert info.value.status == 500
        api.pay_status = 400
        with pytest.raises(BadRequest, match="nope"):
            await client.pay("TST", 10, 1)

    asyncio.run(with_client(api, monkeypatch, scenario))
    assert api.payments == []


def test_bad_credentials_raise_without_secret(monkeypatch):
    api = FakeAPI()

    async def scenario(client):
        with pytest.raises(HTTPException) as info:
            await client.pay("TST", 10, 1)
        assert info.value.status == 401
        assert "wrong-secret" not in str(info.value)

    asyncio.run(with_client(api, monkeypatch, scenario, secret="wrong-secret"))


def test_unstarted_client_refuses_requests():
    client = async_client.AsyncVirtualCryptoClient("cid", "secret", [async_client.Scope.Pay])
    with pytest.raises(RuntimeError):
        asyncio.run(client.pay("TST", 1, 1))
