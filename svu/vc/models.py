"""Values exchanged with VirtualCrypto; the wire carries numbers as strings, these carry ints."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

CLAIM_STATUSES = ("pending", "approved", "denied", "canceled")


def _int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("expected a number, got a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.lstrip("-").isdigit():
        return int(value)
    raise ValueError(f"expected an integer or a decimal string, got {type(value).__name__}")


def _optional_int(value: Any) -> int | None:
    return None if value in (None, "") else _int(value)


def _discord_id(user: Any) -> int | None:
    if not isinstance(user, dict):
        return None
    discord = user.get("discord")
    if not isinstance(discord, dict):
        return None
    return _optional_int(discord.get("id"))


def _datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Currency:
    unit: str
    name: str | None
    guild_id: int | None
    pool_amount: int
    total_amount: int | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Currency:
        return cls(
            unit=str(data["unit"]),
            name=data.get("name"),
            guild_id=_optional_int(data.get("guild")),
            pool_amount=_int(data.get("pool_amount") or 0),
            total_amount=_optional_int(data.get("total_amount")),
        )


@dataclass(frozen=True)
class Claim:
    """A claim as the REST API or a type 2 webhook event describes it; the event carries no claimant."""

    id: int
    status: str
    amount: int
    unit: str
    payer_id: int | None
    claimant_id: int | None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Claim:
        status = str(data["status"])
        if status not in CLAIM_STATUSES:
            raise ValueError(f"unknown claim status {status!r}")
        metadata = data.get("metadata")
        return cls(
            id=_int(data["id"]),
            status=status,
            amount=_int(data["amount"]),
            unit=str(data["currency"]["unit"]),
            payer_id=_discord_id(data.get("payer")),
            claimant_id=_discord_id(data.get("claimant")),
            metadata=metadata if isinstance(metadata, dict) else {},
        )


@dataclass(frozen=True)
class Payment:
    receiver_id: int
    unit: str
    amount: int

    def to_json(self) -> dict[str, str]:
        return {"unit": self.unit, "receiver_discord_id": str(self.receiver_id), "amount": str(self.amount)}


@dataclass(frozen=True)
class IssueResult:
    amount: int
    pool_amount: int
    unit: str


@dataclass(frozen=True)
class AppInfo:
    client_id: str
    public_key: str
    webhook_url: str | None
    subscribed_events: tuple[int, ...]
    grant_types: tuple[str, ...]
    webhook_verified_at: datetime | None
    webhook_failed_at: datetime | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AppInfo:
        return cls(
            client_id=str(data["client_id"]),
            public_key=str(data["public_key"]),
            webhook_url=data.get("webhook_url"),
            subscribed_events=tuple(int(e) for e in data.get("subscribed_events") or ()),
            grant_types=tuple(str(g) for g in data.get("grant_types") or ()),
            webhook_verified_at=_datetime(data.get("webhook_verified_at")),
            webhook_failed_at=_datetime(data.get("webhook_failed_at")),
        )


@dataclass(frozen=True)
class GrantRequest:
    device_code: str
    user_code: str
    expires_in: int


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    expires_in: int
    refresh_token: str | None
    grant_id: str | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TokenSet:
        grant_id = data.get("grant_id")
        return cls(
            access_token=str(data["access_token"]),
            expires_in=_int(data["expires_in"]),
            refresh_token=data.get("refresh_token"),
            grant_id=None if grant_id is None else str(grant_id),
        )
