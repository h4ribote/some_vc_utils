from .structs import Currency, Scope, Claim, ClaimStatus, Balance
from .errors import MissingScope, BadRequest, NotFound, HTTPException
from .client import VirtualCryptoClientBase, VIRTUALCRYPTO_TOKEN_ENDPOINT, VIRTUALCRYPTO_API
from dataclasses import dataclass
from typing import Any, Optional, List
import aiohttp
import asyncio
from time import monotonic

# A token is renewed this many seconds before its stated expiry so that no request is sent with a token about to lapse.
TOKEN_REFRESH_MARGIN_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 30


@dataclass
class APIResponse:
    status: int
    data: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class AsyncVirtualCryptoClient(VirtualCryptoClientBase):
    """Long-lived client: start() opens the HTTP session, the OAuth token is fetched on first use and renewed before expiry or after a 401."""

    def __init__(self, client_id: str, client_secret: str, scopes: List[Scope]):
        super().__init__(client_id, client_secret, scopes)
        self.session: Optional[aiohttp.ClientSession] = None
        self.wait_ready = asyncio.Event()
        self._token_lock = asyncio.Lock()
        self._token_deadline = 0.0

    async def wait_for_ready(self):
        await self.wait_ready.wait()

    async def start(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS))
        self.wait_ready.set()

    async def close(self):
        if self.session is not None and not self.session.closed:
            await self.session.close()

    def _require_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            raise RuntimeError("AsyncVirtualCryptoClient is not started")
        return self.session

    async def set_token(self):
        body = {
            'scope': ' '.join(map(lambda x: x.value, self.scopes)),
            'grant_type': 'client_credentials'
        }
        async with self._require_session().post(
                VIRTUALCRYPTO_TOKEN_ENDPOINT,
                data=body,
                headers={"Authorization": aiohttp.encode_basic_auth(self.client_id, self.client_secret)}) as response:
            status = response.status
            try:
                data = await response.json(content_type=None)
            except ValueError:
                data = None
        if status != 200 or not isinstance(data, dict) or 'access_token' not in data:
            error = data.get('error') if isinstance(data, dict) else None
            raise HTTPException(f"token request failed with HTTP {status}" + (f" ({error})" if error else ""), status)

        self.token = data['access_token']
        self.expires_in = int(data['expires_in'])
        self.token_type = data['token_type']
        self.when_set_token = monotonic()
        self._token_deadline = self.when_set_token + max(0, self.expires_in - TOKEN_REFRESH_MARGIN_SECONDS)

    async def _ensure_token(self, stale_token: Optional[str] = None):
        async with self._token_lock:
            # Another task may have renewed the token while this one waited for the lock.
            if self.token is None or self.token == stale_token or monotonic() >= self._token_deadline:
                await self.set_token()

    async def get_headers(self):
        await self._ensure_token()
        return {
            "Authorization": "Bearer " + self.token
        }

    async def _request(self, method: str, path: str, **kwargs) -> APIResponse:
        """Send a request and read its body; a 401 renews the token and retries once, since the server rejected the request unprocessed."""
        for attempt in range(2):
            headers = await self.get_headers()
            token = self.token
            async with self._require_session().request(method, VIRTUALCRYPTO_API + path, headers=headers, **kwargs) as response:
                status = response.status
                try:
                    data = await response.json(content_type=None)
                except ValueError:
                    data = None
            if status == 401 and attempt == 0:
                await self._ensure_token(stale_token=token)
                continue
            return APIResponse(status, data)
        return APIResponse(status, data)

    @staticmethod
    def _raise_for_status(response: APIResponse, error_key: str = "error_description"):
        if response.ok:
            return
        detail = response.data.get(error_key) if isinstance(response.data, dict) else None
        message = detail or f"HTTP {response.status}"
        if response.status == 400:
            raise BadRequest(message, response.status)
        if response.status == 404:
            raise NotFound(message, response.status)
        raise HTTPException(message, response.status)

    async def get(self, path, params) -> APIResponse:
        return await self._request("GET", path, params=params)

    async def post(self, path, data) -> APIResponse:
        return await self._request("POST", path, data=data)

    async def patch(self, path, data) -> APIResponse:
        return await self._request("PATCH", path, data=data)

    async def get_currency_by_unit(self, unit: str) -> Optional[Currency]:
        response = await self.get("/currencies", {"unit": unit})

        return Currency.by_json(response.data)

    async def get_currency_by_guild(self, guild_id: int) -> Optional[Currency]:
        response = await self.get("/currencies", {"guild": str(guild_id)})

        return Currency.by_json(response.data)

    async def get_currency_by_name(self, name: str) -> Optional[Currency]:
        response = await self.get("/currencies", {"name": name})
        return Currency.by_json(response.data)

    async def get_currency(self, currency_id: int):
        response = await self.get("/currencies/" + str(currency_id), {})

        return Currency.by_json(response.data)

    async def create_user_transaction(self, unit: str, receiver_discord_id: int, amount: int) -> None:
        """Pay `amount` of `unit`; any non-2xx response raises, so a return means the server accepted the payment."""
        if Scope.Pay not in self.scopes:
            raise MissingScope("vc.pay")

        response = await self.post(
            "/users/@me/transactions",
            {
                "unit": unit,
                "receiver_discord_id": str(receiver_discord_id),
                "amount": str(amount)
            }
        )
        self._raise_for_status(response, "error_info")

    pay = create_user_transaction

    async def create_claim(self, payer_discord_id: int, unit: str, amount: int) -> Claim:
        response = await self.post(
            "/users/@me/claims",
            {"payer_discord_id":str(payer_discord_id),"unit":unit,"amount":str(amount)}
        )
        self._raise_for_status(response)
        return Claim.by_json(response.data)

    async def get_claims(self):
        if Scope.Claim not in self.scopes:
            raise MissingScope("vc.claim")

        response = await self.get(
            "/users/@me/claims",
            {}
        )
        self._raise_for_status(response)
        return list(map(Claim.by_json, response.data))

    async def get_claim(self, claim_id: int):
        response = await self.get("/users/@me/claims/" + str(claim_id), {})
        self._raise_for_status(response)
        return Claim.by_json(response.data)

    async def update_claim(self, claim_id: int, status: ClaimStatus):
        if status == ClaimStatus.Pending:
            raise ValueError("can't update to pending")

        response = await self.patch(
            "/users/@me/claims/" + str(claim_id),
            {"status": status.value}
        )
        self._raise_for_status(response, "error_info" if response.status == 400 else "error_description")
        return response

    async def get_balances(self):
        response = await self.get(
            "/users/@me/balances",
            {}
        )
        self._raise_for_status(response)
        return list(map(Balance.by_json, response.data))
