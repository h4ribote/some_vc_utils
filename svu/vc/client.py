"""VirtualCrypto API client: the application's own token from client_credentials, guild tokens for issuing, JSON bodies with numbers as strings, and Idempotency-Key on every money-moving write."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from svu.vc.errors import VCRequestError, VCTransientError, error_from_response
from svu.vc.models import AppInfo, Claim, Currency, GrantRequest, IssueResult, Payment, TokenSet

APP_SCOPES = ("vc.pay", "vc.claim", "oauth2.register")
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
REQUEST_TIMEOUT_SECONDS = 30.0
# The application token is renewed this long before it expires, so no request carries a token about to lapse.
TOKEN_REFRESH_MARGIN_SECONDS = 60
CLAIM_PAGE_LIMIT = 200


def idempotency_header(key: str) -> dict[str, str]:
    """The Idempotency-Key header, whose value the API requires to be a quoted string of at most 256 characters."""
    if not key or len(key) > 256 or '"' in key:
        raise ValueError("an idempotency key is 1 to 256 characters without double quotes")
    return {"Idempotency-Key": f'"{key}"'}


class VCClient:
    """One client per process; start() is implicit and close() releases the HTTP connections."""

    def __init__(self, base_url: str, client_id: str, client_secret: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.AsyncClient(base_url=base_url, timeout=REQUEST_TIMEOUT_SECONDS, transport=transport, headers={"Accept": "application/json", "User-Agent": "some-vc-utils"})
        self._token: str | None = None
        self._token_deadline = 0.0
        self._token_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()

    async def _send(self, method: str, path: str, *, bearer: str | None = None, basic: bool = False, **kwargs: Any) -> httpx.Response:
        headers: dict[str, str] = dict(kwargs.pop("headers", {}))
        if bearer is not None:
            headers["Authorization"] = f"Bearer {bearer}"
        auth = httpx.BasicAuth(self._client_id, self._client_secret) if basic else None
        try:
            return await self._http.request(method, path, headers=headers, auth=auth, **kwargs)
        except httpx.HTTPError as exc:
            raise VCTransientError(f"VirtualCrypto request {method} {path} failed: {type(exc).__name__}") from exc

    @staticmethod
    def _body(response: httpx.Response) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def _check(self, response: httpx.Response) -> Any:
        body = self._body(response)
        if response.is_success:
            return body
        raise error_from_response(response.status_code, body)

    async def _fetch_app_token(self) -> None:
        response = await self._send("POST", "/oauth2/token", basic=True, data={"grant_type": "client_credentials", "scope": " ".join(APP_SCOPES)})
        body = self._check(response)
        if not isinstance(body, dict) or "access_token" not in body:
            raise VCTransientError("token response carried no access_token", status=response.status_code)
        self._token = str(body["access_token"])
        lifetime = int(body.get("expires_in", 0))
        self._token_deadline = time.monotonic() + max(0, lifetime - TOKEN_REFRESH_MARGIN_SECONDS)

    async def _app_token(self, stale: str | None = None) -> str:
        async with self._token_lock:
            if self._token is None or self._token == stale or time.monotonic() >= self._token_deadline:
                await self._fetch_app_token()
            assert self._token is not None
            return self._token

    async def _app(self, method: str, path: str, **kwargs: Any) -> Any:
        """Send with the application token; a 401 renews the token and retries once, since the server rejected the request before doing anything."""
        token = await self._app_token()
        response = await self._send(method, path, bearer=token, **kwargs)
        if response.status_code == 401:
            token = await self._app_token(stale=token)
            response = await self._send(method, path, bearer=token, **kwargs)
        return self._check(response)

    # Payments and claims, as the application's own account.

    async def pay(self, payments: Sequence[Payment], idempotency_key: str) -> None:
        """Pay one or more receivers in one request, which VirtualCrypto applies all or nothing."""
        if not payments:
            raise ValueError("no payments")
        body: Any = payments[0].to_json() if len(payments) == 1 else [p.to_json() for p in payments]
        await self._app("POST", "/api/v2/users/@me/transactions", json=body, headers=idempotency_header(idempotency_key))

    async def create_claim(self, payer_id: int, unit: str, amount: int, metadata: Mapping[str, str]) -> Claim:
        body = {"payer_discord_id": str(payer_id), "unit": unit, "amount": str(amount), "metadata": dict(metadata)}
        return Claim.from_json(await self._app("POST", "/api/v2/users/@me/claims", json=body))

    async def get_claim(self, claim_id: int) -> Claim:
        return Claim.from_json(await self._app("GET", f"/api/v2/users/@me/claims/{claim_id}"))

    async def cancel_claim(self, claim_id: int) -> Claim:
        return Claim.from_json(await self._app("PATCH", f"/api/v2/users/@me/claims/{claim_id}", json={"status": "canceled"}))

    async def list_claimed(self, statuses: Sequence[str], *, after_id: int | None = None) -> list[Claim]:
        """One page of the claims this application made with the given statuses, newest first, continuing below after_id."""
        params: list[tuple[str, str]] = [("type", "claimed"), ("limit", str(CLAIM_PAGE_LIMIT))]
        params += [("statuses[]", status) for status in statuses]
        if after_id is not None:
            params.append(("next", str(after_id)))
        body = await self._app("GET", "/api/v2/users/@me/claims", params=params)
        return [Claim.from_json(item) for item in body or []]

    async def currency_by_unit(self, unit: str) -> Currency | None:
        return await self._currency({"unit": unit})

    async def currency_by_guild(self, guild_id: int) -> Currency | None:
        return await self._currency({"guild": str(guild_id)})

    async def _currency(self, params: dict[str, str]) -> Currency | None:
        response = await self._send("GET", "/api/v2/currencies", params=params)
        if response.status_code == 404:
            return None
        body = self._check(response)
        return Currency.from_json(body) if isinstance(body, dict) else None

    # The application's own registration.

    async def get_app(self) -> AppInfo:
        return AppInfo.from_json(await self._app("GET", "/oauth2/clients/@me"))

    async def update_app(self, changes: Mapping[str, Any]) -> None:
        """PATCH the registration; a changed webhook_url is verified by VirtualCrypto before it is saved."""
        await self._app("PATCH", "/oauth2/clients/@me", json=dict(changes))

    # Guild issuing: the device flow and the guild token.

    async def request_issue_grant(self, guild_id: int) -> GrantRequest:
        body = await self._app("POST", "/oauth2/clients/@me/grant-requests", json={"guild_id": str(guild_id), "scopes": ["vc.issue"]})
        return GrantRequest(device_code=str(body["device_code"]), user_code=str(body["user_code"]), expires_in=int(body["expires_in"]))

    async def poll_device_token(self, device_code: str) -> TokenSet | None:
        """The guild token once the request was approved, None while it is pending; VCRequestError when it expired, was exchanged or was revoked."""
        response = await self._send("POST", "/oauth2/token", basic=True, data={"grant_type": DEVICE_CODE_GRANT, "device_code": device_code})
        body = self._body(response)
        if response.status_code == 400 and isinstance(body, dict) and body.get("error") == "authorization_pending":
            return None
        return TokenSet.from_json(self._check(response))

    async def refresh_guild_token(self, refresh_token: str) -> TokenSet:
        response = await self._send("POST", "/oauth2/token", basic=True, data={"grant_type": "refresh_token", "refresh_token": refresh_token})
        return TokenSet.from_json(self._check(response))

    async def issue(self, guild_token: str, receiver_id: int, amount: int, idempotency_key: str) -> IssueResult:
        """Issue from the guild's pool; a 401 or 403 means the grant no longer allows issuing and is a VCRequestError."""
        body = {"receiver_discord_id": str(receiver_id), "amount": str(amount)}
        response = await self._send("POST", "/api/v2/currencies/issue", bearer=guild_token, json=body, headers=idempotency_header(idempotency_key))
        if response.status_code in (401, 403):
            data = self._body(response)
            code = data.get("error") if isinstance(data, dict) else None
            raise VCRequestError(f"VirtualCrypto refused the guild token (HTTP {response.status_code})", status=response.status_code, error=code, code="grant_revoked")
        data = self._check(response)
        return IssueResult(amount=int(data["amount"]), pool_amount=int(data["pool_amount"]), unit=str(data["unit"]))
