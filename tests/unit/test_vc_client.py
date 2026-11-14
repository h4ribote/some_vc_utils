from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest

from svu.vc import Payment, VCClient, VCRequestError, VCTransientError
from svu.vc.client import idempotency_header
from svu.vc.errors import error_from_response
from tests.fake_vc import APP_DISCORD_ID, CLIENT_ID, CLIENT_SECRET, FakeVC


@pytest.fixture
def fake() -> FakeVC:
    vc = FakeVC()
    vc.add_currency("nyan", guild_id=10, pool=1000)
    vc.give(APP_DISCORD_ID, "nyan", 500)
    return vc


@pytest.fixture
async def client(fake: FakeVC) -> AsyncIterator[VCClient]:
    c = VCClient("https://vc.test", CLIENT_ID, CLIENT_SECRET, transport=fake.transport())
    yield c
    await c.close()


def payment_requests(fake: FakeVC) -> list[httpx.Request]:
    return [r for r in fake.requests if r.url.path == "/api/v2/users/@me/transactions"]


async def test_pay_sends_json_with_string_numbers_and_a_quoted_key(client: VCClient, fake: FakeVC) -> None:
    await client.pay([Payment(receiver_id=123, unit="nyan", amount=30)], "key-1")
    [request] = payment_requests(fake)
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Idempotency-Key"] == '"key-1"'
    assert json.loads(request.content) == {"unit": "nyan", "receiver_discord_id": "123", "amount": "30"}
    assert fake.balance(123, "nyan") == 30


async def test_bulk_payment_is_one_request_and_all_or_nothing(client: VCClient, fake: FakeVC) -> None:
    await client.pay([Payment(1, "nyan", 100), Payment(2, "nyan", 100)], "bulk")
    assert isinstance(json.loads(payment_requests(fake)[0].content), list)
    with pytest.raises(VCRequestError) as info:
        await client.pay([Payment(3, "nyan", 200), Payment(4, "nyan", 200)], "too-much")
    assert info.value.code == "not_enough_amount"
    assert fake.balance(3, "nyan") == 0


async def test_same_key_does_not_pay_twice(client: VCClient, fake: FakeVC) -> None:
    await client.pay([Payment(1, "nyan", 10)], "same")
    await client.pay([Payment(1, "nyan", 10)], "same")
    assert fake.balance(1, "nyan") == 10


async def test_the_app_token_is_reused_and_renewed_after_a_401(client: VCClient, fake: FakeVC) -> None:
    await client.get_app()
    await client.get_app()
    assert fake.token_requests == 1
    fake.force("GET /oauth2/clients/@me", 401, {"error": "invalid_token"})
    await client.get_app()
    assert fake.token_requests == 2


async def test_connection_failures_and_server_errors_are_transient(client: VCClient, fake: FakeVC) -> None:
    fake.force("POST /api/v2/users/@me/transactions", 0)
    with pytest.raises(VCTransientError):
        await client.pay([Payment(1, "nyan", 1)], "a")
    fake.force("POST /api/v2/users/@me/transactions", 503, {"error": "server_error"})
    with pytest.raises(VCTransientError):
        await client.pay([Payment(1, "nyan", 1)], "b")


def test_error_classification() -> None:
    assert error_from_response(400, {"error": "invalid_request", "error_info": "not_found_currency"}).definitive
    assert error_from_response(409, {"error": "conflict", "error_info": "not_enough_amount"}).definitive
    processing = error_from_response(409, {"error": "processing", "error_description": "should_retry_after_in_seconds"})
    assert not processing.definitive
    assert not error_from_response(429, {"error": "rate_limit_exceeded"}).definitive
    assert not error_from_response(504, {"error": "request_timeout"}).definitive
    assert error_from_response(400, {"error": "invalid_request", "error_description": "invalid_amount"}).code == "invalid_amount"


def test_idempotency_keys_are_validated() -> None:
    assert idempotency_header("abc") == {"Idempotency-Key": '"abc"'}
    for bad in ("", "a" * 257, 'a"b'):
        with pytest.raises(ValueError):
            idempotency_header(bad)


async def test_claims_round_trip_with_metadata(client: VCClient, fake: FakeVC) -> None:
    claim = await client.create_claim(55, "nyan", 70, {"svu_operation": "op-1"})
    assert claim.status == "pending" and claim.amount == 70 and claim.payer_id == 55 and claim.claimant_id == APP_DISCORD_ID
    assert claim.metadata == {"svu_operation": "op-1"}
    fake.give(55, "nyan", 70)
    fake.approve(claim.id)
    assert (await client.get_claim(claim.id)).status == "approved"
    [listed] = await client.list_claimed(["approved"])
    assert listed.id == claim.id
    with pytest.raises(VCRequestError) as info:
        await client.cancel_claim(claim.id)
    assert info.value.code == "invalid_status"


async def test_currency_lookup(client: VCClient) -> None:
    currency = await client.currency_by_unit("nyan")
    assert currency is not None and currency.guild_id == 10
    assert await client.currency_by_unit("missing") is None
    assert (await client.currency_by_guild(10)) == currency


async def test_device_flow_and_issue(client: VCClient, fake: FakeVC) -> None:
    request = await client.request_issue_grant(10)
    assert await client.poll_device_token(request.device_code) is None
    fake.approve_grant(request.user_code)
    tokens = await client.poll_device_token(request.device_code)
    assert tokens is not None and tokens.refresh_token and tokens.grant_id == "42"
    result = await client.issue(tokens.access_token, 7, 40, "issue-1")
    assert (result.amount, result.pool_amount, result.unit) == (40, 960, "nyan")
    refreshed = await client.refresh_guild_token(tokens.refresh_token)
    with pytest.raises(VCRequestError):
        await client.refresh_guild_token(tokens.refresh_token)
    fake.revoke_guild(10)
    with pytest.raises(VCRequestError) as info:
        await client.issue(refreshed.access_token, 7, 1, "issue-2")
    assert info.value.code == "grant_revoked"
    with pytest.raises(VCRequestError):
        await client.poll_device_token(request.device_code)
