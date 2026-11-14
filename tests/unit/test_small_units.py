from __future__ import annotations

import asyncio
import io
import json
import logging

import httpx
import pytest

from svu.api.discord_oauth import can_manage
from svu.crypto import InvalidToken, SecretBox, token_digest
from svu.discord_rest import DiscordError, DiscordRest
from svu.logging import JsonFormatter, register_secret
from svu.worker.scheduler import Job, run_jobs


def test_secret_box_round_trips_and_depends_on_the_key() -> None:
    box = SecretBox("a" * 40)
    sealed = box.encrypt("token-value")
    assert sealed != "token-value"
    assert box.decrypt(sealed) == "token-value"
    with pytest.raises(InvalidToken):
        SecretBox("b" * 40).decrypt(sealed)


def test_token_digest_is_stable_and_not_the_token() -> None:
    assert token_digest("x") == token_digest("x") != "x"


def test_can_manage() -> None:
    assert can_manage({"owner": True, "permissions": "0"})
    assert can_manage({"owner": False, "permissions": str(0x20)})
    assert can_manage({"owner": False, "permissions": str(0x8)})
    assert not can_manage({"owner": False, "permissions": str(0x400)})
    assert not can_manage({"permissions": "garbage"})


def test_log_lines_redact_secrets_and_sensitive_extras() -> None:
    register_secret("super-secret-value")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("svu.test.redaction")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("token is super-secret-value", extra={"access_token": "abc", "detail": {"password": "p"}, "ok": 1})
    finally:
        logger.removeHandler(handler)
    line = json.loads(stream.getvalue())
    assert "super-secret-value" not in stream.getvalue()
    assert line["access_token"] == "***" and line["detail"] == {"password": "***"} and line["ok"] == 1


async def test_discord_rest_marks_unrecoverable_failures_gone() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/v10/webhooks/"):
            return httpx.Response(404, json={"message": "Unknown Webhook"})
        if request.url.path == "/api/v10/users/@me/channels":
            return httpx.Response(200, json={"id": "55"})
        return httpx.Response(429, json={"retry_after": 2.5})

    rest = DiscordRest("bot-token", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(DiscordError) as gone:
            await rest.edit_original_response(1, "interaction-token", {"content": "x"})
        assert gone.value.gone and "interaction-token" not in str(gone.value)
        with pytest.raises(DiscordError) as limited:
            await rest.send_dm(5, {"content": "x"})
        assert not limited.value.gone and limited.value.retry_after == 2.5
    finally:
        await rest.close()


async def test_a_failing_job_keeps_running_and_does_not_stop_others() -> None:
    calls = {"bad": 0, "good": 0}
    stop = asyncio.Event()

    async def bad() -> None:
        calls["bad"] += 1
        raise RuntimeError("boom")

    async def good() -> None:
        calls["good"] += 1
        if calls["good"] >= 3 and calls["bad"] >= 3:
            stop.set()

    await asyncio.wait_for(run_jobs([Job("bad", 0.01, bad), Job("good", 0.01, good)], stop, drain_seconds=1), timeout=5)
    assert calls["bad"] >= 3 and calls["good"] >= 3
