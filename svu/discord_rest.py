"""The few Discord REST calls the worker makes without a gateway connection: editing an interaction's original response and sending direct messages."""

from __future__ import annotations

from typing import Any

import httpx

API_BASE = "https://discord.com/api/v10"
REQUEST_TIMEOUT_SECONDS = 15.0


class DiscordError(Exception):
    """A failed Discord request; gone is True when retrying cannot succeed (unknown webhook, unknown user, DMs closed)."""

    def __init__(self, message: str, *, status: int | None, gone: bool, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.gone = gone
        self.retry_after = retry_after


class DiscordRest:
    def __init__(self, bot_token: str, *, base_url: str = API_BASE, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            transport=transport,
            headers={"User-Agent": "DiscordBot (https://github.com/h4ribote/some_vc_utils, 2)"},
        )
        self._bot_auth = {"Authorization": f"Bot {bot_token}"}

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, *, authorized: bool, json: Any) -> Any:
        try:
            response = await self._http.request(method, path, json=json, headers=self._bot_auth if authorized else None)
        except httpx.HTTPError as exc:
            raise DiscordError(f"Discord request failed: {type(exc).__name__}", status=None, gone=False) from exc
        if response.is_success:
            return response.json() if response.content else None
        retry_after = None
        if response.status_code == 429:
            try:
                retry_after = float(response.json().get("retry_after", 1.0))
            except (ValueError, AttributeError):
                retry_after = 1.0
        # 403 and 404 do not change on retry: the interaction token expired, the user is unknown or does not accept DMs from the bot.
        gone = response.status_code in (400, 403, 404)
        raise DiscordError(f"Discord answered HTTP {response.status_code} to {method} {path.split('/')[1]}", status=response.status_code, gone=gone, retry_after=retry_after)

    async def edit_original_response(self, application_id: int, interaction_token: str, message: dict[str, Any]) -> None:
        await self._request("PATCH", f"/webhooks/{application_id}/{interaction_token}/messages/@original", authorized=False, json=message)

    async def send_dm(self, user_id: int, message: dict[str, Any]) -> None:
        channel = await self._request("POST", "/users/@me/channels", authorized=True, json={"recipient_id": str(user_id)})
        await self._request("POST", f"/channels/{channel['id']}/messages", authorized=True, json={**message, "allowed_mentions": {"parse": []}})
