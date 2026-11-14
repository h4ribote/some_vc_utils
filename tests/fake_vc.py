"""An in-process VirtualCrypto v3 for tests, served through httpx.MockTransport: JSON bodies with numbers as strings, idempotency keys, all-or-nothing bulk payments, claims, guild issuing with the device flow, and signed webhook bodies."""

from __future__ import annotations

import base64
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

APP_DISCORD_ID = 900_000_000_000_000_001
CLIENT_ID = "test-client"
CLIENT_SECRET = "test-secret"
TOKEN_LIFETIME = 3600


def _json(status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


@dataclass
class FakeClaim:
    id: int
    claimant_id: int
    payer_id: int
    unit: str
    amount: int
    status: str = "pending"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "currency": {"name": self.unit, "unit": self.unit, "guild": "1", "pool_amount": "0"},
            "amount": str(self.amount),
            "claimant": {"id": "1", "discord": {"id": str(self.claimant_id)}},
            "payer": {"id": "2", "discord": {"id": str(self.payer_id)}},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "status": self.status,
            "metadata": self.metadata,
        }

    def to_event(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "amount": str(self.amount),
            "updated_at": "2026-01-01T00:00:00Z",
            "metadata": self.metadata,
            "payer": {"id": 2, "discord": {"id": str(self.payer_id)}},
            "currency": {"id": 1, "unit": self.unit, "name": self.unit, "guild": "1", "pool_amount": "0"},
        }


@dataclass
class FakeVC:
    balances: dict[tuple[int, str], int] = field(default_factory=dict)
    currencies: dict[str, int] = field(default_factory=dict)
    pools: dict[int, int] = field(default_factory=dict)
    claims: dict[int, FakeClaim] = field(default_factory=dict)
    idempotency: dict[str, tuple[int, Any]] = field(default_factory=dict)
    grant_requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    guild_tokens: dict[str, int] = field(default_factory=dict)
    refresh_tokens: dict[str, int] = field(default_factory=dict)
    app: dict[str, Any] = field(default_factory=dict)
    # Responses forced for the next requests whose "METHOD path" starts with the key, consumed in order.
    forced: list[tuple[str, int, Any]] = field(default_factory=list)
    # Requests whose "METHOD path" starts with one of these are processed, then answered with a connection failure, once per entry.
    dropped: list[str] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    token_requests: int = 0
    payment_calls: int = 0
    _next_claim: int = 1
    _signing_key: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)

    def __post_init__(self) -> None:
        self.app = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "public_key": self.public_key_hex,
            "webhook_url": None,
            "webhook_verified_at": None,
            "webhook_failed_at": None,
            "subscribed_events": [2, 3, 4],
            "grant_types": ["authorization_code"],
        }

    # Test helpers.

    @property
    def public_key_hex(self) -> str:
        return self._signing_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def balance(self, user_id: int, unit: str) -> int:
        return self.balances.get((user_id, unit), 0)

    def give(self, user_id: int, unit: str, amount: int) -> None:
        self.balances[(user_id, unit)] = self.balance(user_id, unit) + amount

    def add_currency(self, unit: str, guild_id: int, pool: int = 0) -> None:
        self.currencies[unit] = guild_id
        self.pools[guild_id] = pool

    def force(self, prefix: str, status: int, body: Any = None) -> None:
        self.forced.append((prefix, status, body))

    def approve(self, claim_id: int) -> FakeClaim:
        claim = self.claims[claim_id]
        assert claim.status == "pending"
        if self.balance(claim.payer_id, claim.unit) < claim.amount:
            raise AssertionError("payer cannot cover the claim")
        self.give(claim.payer_id, claim.unit, -claim.amount)
        self.give(claim.claimant_id, claim.unit, claim.amount)
        claim.status = "approved"
        return claim

    def deny(self, claim_id: int) -> FakeClaim:
        claim = self.claims[claim_id]
        claim.status = "denied"
        return claim

    def claim_event(self, *claims: FakeClaim) -> dict[str, Any]:
        return {"type": 2, "data": [c.to_event() for c in claims]}

    def sign(self, body: bytes, timestamp: int | None = None) -> dict[str, str]:
        ts = str(int(time.time()) if timestamp is None else timestamp)
        signature = self._signing_key.sign(ts.encode() + body).hex()
        return {"X-Signature-Ed25519": signature, "X-Signature-Timestamp": ts}

    def approve_grant(self, user_code: str) -> str:
        """Approve the pending request with user_code and return its device code."""
        for device_code, request in self.grant_requests.items():
            if request["user_code"] == user_code:
                request["status"] = "approved"
                return device_code
        raise KeyError(user_code)

    def revoke_guild(self, guild_id: int) -> None:
        self.guild_tokens = {t: g for t, g in self.guild_tokens.items() if g != guild_id}
        self.refresh_tokens = {t: g for t, g in self.refresh_tokens.items() if g != guild_id}

    def expire_guild_tokens(self) -> None:
        self.guild_tokens.clear()

    # The HTTP side.

    def handle(self, request: httpx.Request) -> httpx.Response:
        response = self._route(request)
        route = f"{request.method} {request.url.path}"
        for index, prefix in enumerate(self.dropped):
            if route.startswith(prefix):
                del self.dropped[index]
                raise httpx.ReadTimeout("forced lost response", request=request)
        return response

    def _route(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = f"{request.method} {request.url.path}"
        for index, (prefix, status, body) in enumerate(self.forced):
            if route.startswith(prefix):
                del self.forced[index]
                if status == 0:
                    raise httpx.ConnectError("forced connection failure", request=request)
                return _json(status, body)
        if request.url.path == "/oauth2/token":
            return self._token(request)
        if route == "GET /oauth2/clients/@me":
            return self._authorized(request) or _json(200, self.app)
        if route == "PATCH /oauth2/clients/@me":
            return self._authorized(request) or self._patch_app(request)
        if route == "POST /oauth2/clients/@me/grant-requests":
            return self._authorized(request) or self._grant_request(request)
        if route == "POST /api/v2/users/@me/transactions":
            return self._authorized(request) or self._idempotent(request, self._pay)
        if route == "POST /api/v2/users/@me/claims":
            return self._authorized(request) or self._create_claim(request)
        if route == "GET /api/v2/users/@me/claims":
            return self._authorized(request) or self._list_claims(request)
        if request.url.path.startswith("/api/v2/users/@me/claims/"):
            return self._authorized(request) or self._claim(request)
        if route == "GET /api/v2/currencies":
            return self._currency(request)
        if route == "POST /api/v2/currencies/issue":
            return self._issue(request)
        return _json(404, {"error": "not_found"})

    def _authorized(self, request: httpx.Request) -> httpx.Response | None:
        if request.headers.get("Authorization") != "Bearer app-token":
            return _json(401, {"error": "invalid_token"})
        return None

    def _basic_ok(self, request: httpx.Request) -> bool:
        expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        return bool(request.headers.get("Authorization") == expected)

    def _token(self, request: httpx.Request) -> httpx.Response:
        if not self._basic_ok(request):
            return _json(400, {"error": "invalid_client"})
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        grant = form.get("grant_type")
        if grant == "client_credentials":
            self.token_requests += 1
            return _json(200, {"access_token": "app-token", "expires_in": TOKEN_LIFETIME, "token_type": "Bearer"})
        if grant == "urn:ietf:params:oauth:grant-type:device_code":
            req = self.grant_requests.get(form.get("device_code", ""))
            if req is None or req["status"] == "exchanged":
                return _json(400, {"error": "invalid_grant", "error_description": "invalid_device_code"})
            if req["status"] == "pending":
                return _json(400, {"error": "authorization_pending"})
            req["status"] = "exchanged"
            return _json(200, self._mint(req["guild_id"], grant_id="42"))
        if grant == "refresh_token":
            guild = self.refresh_tokens.pop(form.get("refresh_token", ""), None)
            if guild is None:
                return _json(400, {"error": "invalid_grant", "error_description": "invalid_refresh_token"})
            return _json(200, self._mint(guild))
        return _json(400, {"error": "unsupported_grant_type"})

    def _mint(self, guild_id: int, grant_id: str | None = None) -> dict[str, Any]:
        access, refresh = f"guild-{uuid.uuid4()}", f"refresh-{uuid.uuid4()}"
        self.guild_tokens[access] = guild_id
        self.refresh_tokens[refresh] = guild_id
        body: dict[str, Any] = {"access_token": access, "token_type": "Bearer", "expires_in": TOKEN_LIFETIME, "refresh_token": refresh}
        if grant_id is not None:
            body["grant_id"] = grant_id
        return body

    def _patch_app(self, request: httpx.Request) -> httpx.Response:
        self.app.update(json.loads(request.content))
        return httpx.Response(204)

    def _grant_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        device_code = str(uuid.uuid4())
        user_code = uuid.uuid4().hex[:8].upper()
        self.grant_requests[device_code] = {"guild_id": int(body["guild_id"]), "user_code": user_code, "status": "pending"}
        return _json(201, {"device_code": device_code, "user_code": user_code, "verification_uri": "discord", "expires_in": 600})

    def _idempotent(self, request: httpx.Request, handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Response:
        raw = request.headers.get("Idempotency-Key")
        if raw is None:
            return handler(request)
        if not (raw.startswith('"') and raw.endswith('"')):
            return _json(400, {"error": "invalid_request", "error_description": "invalid_idempotency_key"})
        key = f"{request.url.path}:{raw}"
        if key in self.idempotency:
            status, body = self.idempotency[key]
            return _json(status, body, {"Idempotency-Status": "Duplicate"})
        response: httpx.Response = handler(request)
        if response.status_code < 500:
            self.idempotency[key] = (response.status_code, response.json())
        return response

    def _pay(self, request: httpx.Request) -> httpx.Response:
        self.payment_calls += 1
        if request.headers.get("Content-Type") != "application/json":
            return _json(415, {"error": "unsupported_media_type"})
        body = json.loads(request.content)
        items = body if isinstance(body, list) else [body]
        for item in items:
            if not all(isinstance(item.get(k), str) for k in ("unit", "receiver_discord_id", "amount")):
                return _json(400, {"error": "invalid_request", "error_description": "invalid_type_of_variable"})
            if item["unit"] not in self.currencies:
                return _json(400, {"error": "invalid_request", "error_info": "not_found_currency"})
        needed: dict[str, int] = {}
        for item in items:
            needed[item["unit"]] = needed.get(item["unit"], 0) + int(item["amount"])
        if any(self.balance(APP_DISCORD_ID, unit) < amount for unit, amount in needed.items()):
            return _json(409, {"error": "conflict", "error_info": "not_enough_amount"})
        for item in items:
            self.give(APP_DISCORD_ID, item["unit"], -int(item["amount"]))
            self.give(int(item["receiver_discord_id"]), item["unit"], int(item["amount"]))
        return _json(201, {})

    def _create_claim(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["unit"] not in self.currencies:
            return _json(400, {"error": "invalid_request", "error_description": "not_found_currency"})
        claim = FakeClaim(id=self._next_claim, claimant_id=APP_DISCORD_ID, payer_id=int(body["payer_discord_id"]), unit=body["unit"], amount=int(body["amount"]), metadata=body.get("metadata") or {})
        self._next_claim += 1
        self.claims[claim.id] = claim
        return _json(201, claim.to_json())

    def _list_claims(self, request: httpx.Request) -> httpx.Response:
        statuses = request.url.params.get_list("statuses[]") or ["pending"]
        after = request.url.params.get("next")
        limit = int(request.url.params.get("limit", "50"))
        items = sorted((c for c in self.claims.values() if c.status in statuses), key=lambda c: -c.id)
        if after is not None:
            items = [c for c in items if c.id < int(after)]
        return _json(200, [c.to_json() for c in items[:limit]])

    def _claim(self, request: httpx.Request) -> httpx.Response:
        claim_id = int(request.url.path.rsplit("/", 1)[1])
        claim = self.claims.get(claim_id)
        if claim is None:
            return _json(404, {"error": "not_found", "error_description": "not_found"})
        if request.method == "GET":
            return _json(200, claim.to_json())
        status = json.loads(request.content).get("status")
        if claim.status != "pending":
            return _json(409, {"error": "conflict", "error_info": "invalid_status"})
        claim.status = status
        return _json(200, claim.to_json())

    def _currency(self, request: httpx.Request) -> httpx.Response:
        unit = request.url.params.get("unit")
        guild = request.url.params.get("guild")
        for name, guild_id in self.currencies.items():
            if name == unit or (guild is not None and str(guild_id) == guild):
                return _json(200, {"unit": name, "name": name, "guild": str(guild_id), "pool_amount": str(self.pools.get(guild_id, 0)), "total_amount": "0"})
        return _json(404, {"error": "not_found", "error_description": "not_found"})

    def _issue(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        guild_id = self.guild_tokens.get(auth.removeprefix("Bearer "))
        if guild_id is None:
            return _json(401, {"error": "invalid_token"})

        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            amount = int(body["amount"])
            if self.pools.get(guild_id, 0) < amount:
                return _json(409, {"error": "conflict", "error_info": "not_enough_amount"})
            unit = next(u for u, g in self.currencies.items() if g == guild_id)
            self.pools[guild_id] -= amount
            self.give(int(body["receiver_discord_id"]), unit, amount)
            return _json(201, {"amount": str(amount), "pool_amount": str(self.pools[guild_id]), "unit": unit})

        return self._idempotent(request, handler)
