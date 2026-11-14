"""Discord OAuth2 for the dashboard: the authorization URL, the code exchange and the two reads a session needs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
API_BASE = "https://discord.com/api/v10"
SCOPES = "identify guilds"
CALLBACK_PATH = "/auth/callback"

PERMISSION_ADMINISTRATOR = 0x8
PERMISSION_MANAGE_GUILD = 0x20


class OAuthError(Exception):
    """Discord refused the code or the token; unauthorized is True when the user's token is no longer valid."""

    def __init__(self, message: str, *, unauthorized: bool = False) -> None:
        super().__init__(message)
        self.unauthorized = unauthorized


@dataclass(frozen=True)
class DiscordUser:
    id: int
    username: str
    avatar: str | None


def can_manage(guild: dict[str, Any]) -> bool:
    """Whether the user may manage the guild as /users/@me/guilds describes it: its owner, or Manage Server or Administrator."""
    if guild.get("owner"):
        return True
    try:
        permissions = int(guild.get("permissions", "0"))
    except (TypeError, ValueError):
        return False
    return bool(permissions & (PERMISSION_ADMINISTRATOR | PERMISSION_MANAGE_GUILD))


class DiscordOAuth:
    def __init__(self, client_id: int, client_secret: str, redirect_uri: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.client_id = client_id
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._http = httpx.AsyncClient(base_url=API_BASE, timeout=15.0, transport=transport)

    async def close(self) -> None:
        await self._http.aclose()

    def authorize_url(self, state: str) -> str:
        query = {"client_id": str(self.client_id), "response_type": "code", "scope": SCOPES, "redirect_uri": self.redirect_uri, "state": state, "prompt": "none"}
        return f"{AUTHORIZE_URL}?{urlencode(query)}"

    async def exchange(self, code: str) -> str:
        """The user's access token for an authorization code."""
        try:
            response = await self._http.post(
                "/oauth2/token",
                data={"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri},
                auth=httpx.BasicAuth(str(self.client_id), self._client_secret),
            )
        except httpx.HTTPError as exc:
            raise OAuthError(f"token exchange failed: {type(exc).__name__}") from exc
        if not response.is_success:
            raise OAuthError(f"token exchange answered HTTP {response.status_code}")
        return str(response.json()["access_token"])

    async def _get(self, path: str, token: str) -> Any:
        try:
            response = await self._http.get(path, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            raise OAuthError(f"Discord request failed: {type(exc).__name__}") from exc
        if response.status_code == 401:
            raise OAuthError("the Discord token is no longer valid", unauthorized=True)
        if not response.is_success:
            raise OAuthError(f"Discord answered HTTP {response.status_code}")
        return response.json()

    async def user(self, token: str) -> DiscordUser:
        data = await self._get("/users/@me", token)
        return DiscordUser(id=int(data["id"]), username=str(data.get("global_name") or data["username"]), avatar=data.get("avatar"))

    async def guilds(self, token: str) -> list[dict[str, Any]]:
        data = await self._get("/users/@me/guilds", token)
        return [{"id": str(g["id"]), "name": str(g["name"]), "icon": g.get("icon"), "owner": bool(g.get("owner")), "permissions": str(g.get("permissions", "0"))} for g in data]
