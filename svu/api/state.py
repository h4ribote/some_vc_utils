"""What the API's routes share, held on app.state for the life of the process."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine

from svu.api.discord_oauth import DiscordOAuth
from svu.config import Settings
from svu.core import webhooks
from svu.crypto import SecretBox
from svu.vc import VCClient

# The webhook public key is re-read from the database at most this often, and at once when a signature does not verify.
KEY_CACHE_SECONDS = 60.0


@dataclass
class PublicKeyCache:
    _key: str | None = None
    _loaded_at: float = field(default=float("-inf"))

    async def get(self, engine: AsyncEngine, *, reload: bool = False) -> str | None:
        if reload or self._key is None or time.monotonic() - self._loaded_at > KEY_CACHE_SECONDS:
            async with engine.connect() as conn:
                self._key = await webhooks.public_key(conn)
            self._loaded_at = time.monotonic()
        return self._key


@dataclass
class ApiState:
    settings: Settings
    engine: AsyncEngine
    vc: VCClient
    box: SecretBox
    oauth: DiscordOAuth
    keys: PublicKeyCache = field(default_factory=PublicKeyCache)


def api_state(request: Request) -> ApiState:
    return cast(ApiState, cast(Any, request.app.state).svu)
