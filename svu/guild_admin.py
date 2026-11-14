"""Guild administration shared by the /reward_pool commands and the dashboard; every change is written to the audit log."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from svu.core import audit, grants, guilds, rewards
from svu.crypto import SecretBox
from svu.vc import VCClient, VCError


class AdminError(Exception):
    """A refused administrative change; the message is shown to the administrator."""


@dataclass(frozen=True)
class IssueRequest:
    """The code the administrator approves with /grant approve, or None when an active grant already covers the guild."""

    user_code: str | None
    expires_at: datetime | None
    unit: str | None


async def set_reward_unit(engine: AsyncEngine, vc: VCClient, guild_id: int, unit: str, actor_id: int) -> None:
    try:
        currency = await vc.currency_by_unit(unit)
    except VCError as exc:
        raise AdminError("VirtualCryptoに接続できませんでした。しばらくしてからもう一度お試しください。") from exc
    if currency is None:
        raise AdminError(f"通貨 `{unit}` は存在しません。")
    async with engine.begin() as conn:
        settings = await guilds.settings(conn, guild_id)
        if settings.funding_source == "issue":
            raise AdminError("支払い元が発行枠のときは、報酬の通貨はサーバーの通貨に固定されます。")
        await guilds.set_reward_unit(conn, guild_id, currency.unit)
        await audit.record(conn, "reward_unit.set", actor_id=actor_id, guild_id=guild_id, unit=currency.unit)


async def use_wallet(engine: AsyncEngine, guild_id: int, actor_id: int) -> None:
    async with engine.begin() as conn:
        await guilds.set_funding_source(conn, guild_id, "wallet")
        await audit.record(conn, "funding_source.set", actor_id=actor_id, guild_id=guild_id, source="wallet")


async def use_issue(engine: AsyncEngine, vc: VCClient, box: SecretBox, guild_id: int, actor_id: int) -> IssueRequest:
    """Switch to issuing when the guild already granted it, otherwise ask VirtualCrypto for a grant request and return its code."""
    async with engine.begin() as conn:
        grant = await grants.active_grant(conn, guild_id)
        if grant is not None:
            await guilds.set_funding_source(conn, guild_id, "issue", reward_unit=grant.unit)
            await audit.record(conn, "funding_source.set", actor_id=actor_id, guild_id=guild_id, source="issue")
            return IssueRequest(user_code=None, expires_at=None, unit=grant.unit)
    try:
        currency = await vc.currency_by_guild(guild_id)
        if currency is None:
            raise AdminError("このサーバーにはVirtualCryptoの通貨がありません。先にVirtualCryptoの `/create` で通貨を作成してください。")
        request = await vc.request_issue_grant(guild_id)
    except VCError as exc:
        raise AdminError("VirtualCryptoに発行許可を申請できませんでした。しばらくしてからもう一度お試しください。") from exc
    expires_at = datetime.now(UTC) + timedelta(seconds=request.expires_in)
    async with engine.begin() as conn:
        await grants.create_request(conn, guild_id, actor_id, box.encrypt(request.device_code), request.user_code, expires_at)
        await audit.record(conn, "issue_grant.requested", actor_id=actor_id, guild_id=guild_id)
    return IssueRequest(user_code=request.user_code, expires_at=expires_at, unit=currency.unit)


async def withdraw(engine: AsyncEngine, guild_id: int, unit: str, amount: int, actor_id: int) -> None:
    async with engine.begin() as conn:
        if await rewards.withdraw(conn, guild_id, unit, amount, actor_id) is None:
            raise AdminError("プールの残高が足りません。")
        await audit.record(conn, "pool.withdraw", actor_id=actor_id, guild_id=guild_id, unit=unit, amount=amount)


async def set_rule(engine: AsyncEngine, guild_id: int, reward_type: str, amount: int, cooldown_seconds: int, actor_id: int) -> None:
    try:
        guilds.validate_rule(reward_type, amount, cooldown_seconds)
    except ValueError as exc:
        raise AdminError("報酬の設定が正しくありません。") from exc
    async with engine.begin() as conn:
        await guilds.set_rule(conn, guild_id, reward_type, amount, cooldown_seconds)
        await audit.record(conn, "rule.set", actor_id=actor_id, guild_id=guild_id, reward_type=reward_type, amount=amount, cooldown_seconds=cooldown_seconds)


async def delete_rule(engine: AsyncEngine, guild_id: int, reward_type: str, actor_id: int) -> bool:
    async with engine.begin() as conn:
        deleted = await guilds.delete_rule(conn, guild_id, reward_type)
        if deleted:
            await audit.record(conn, "rule.delete", actor_id=actor_id, guild_id=guild_id, reward_type=reward_type)
    return deleted
