"""The worker's jobs: payouts, the webhook inbox, Discord messages, claim reconciliation and expiry, guild issuing grants, app sync, backups and pruning."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from svu import backup, render
from svu.config import Settings
from svu.core import grants, guilds, operations, payouts, webhooks
from svu.core.messages import TEMPLATE_SEND_RECEIVED
from svu.crypto import SecretBox
from svu.db.tables import dashboard_sessions, discord_messages, oauth_states
from svu.discord_rest import DiscordError, DiscordRest
from svu.vc import Payment, VCClient, VCError, VCRequestError
from svu.vc.webhook import EVENT_CLAIM_UPDATE, EVENT_GRANT_DECISION

log = logging.getLogger("svu.worker")

WEBHOOK_PATH = "/vc/webhook"
SUBSCRIBED_EVENTS = (EVENT_CLAIM_UPDATE, EVENT_GRANT_DECISION)
# Units of work handled per tick, so one busy job does not hold its loop indefinitely.
MAX_PER_TICK = 50
# An operation left in creating this long is looked up by its claim metadata, and failed when no claim carries it after CREATING_GIVE_UP.
CREATING_GRACE = timedelta(seconds=60)
CREATING_GIVE_UP = timedelta(minutes=5)
# Claim list pages scanned when looking for an operation's claim by metadata.
CLAIM_SCAN_PAGES = 5
# A guild token is refreshed this long before it expires.
TOKEN_REFRESH_MARGIN = timedelta(minutes=10)
GRANT_POLL_INTERVAL = timedelta(seconds=15)
MESSAGE_MAX_ATTEMPTS = 8
# An interaction response is edited only while its token has at least this much life left.
INTERACTION_MARGIN = timedelta(seconds=30)
WEBHOOK_MAX_ATTEMPTS = 10
RETENTION = timedelta(days=30)
OAUTH_STATE_LIFETIME = timedelta(minutes=10)


def utcnow() -> datetime:
    return datetime.now(UTC)


async def sync_vc_app(engine: AsyncEngine, vc: VCClient, public_base_url: str | None) -> list[str]:
    """Record the app's public key, then point its webhook at this deployment, subscribe to claim and grant events and enable refresh tokens; returns the fields changed.

    The key is stored before the webhook changes, because VirtualCrypto verifies the new webhook with a signed PING during the change.
    """
    app = await vc.get_app()
    async with engine.begin() as conn:
        await webhooks.save_app_state(conn, public_key=app.public_key, webhook_url=app.webhook_url, webhook_verified_at=app.webhook_verified_at, webhook_failed_at=app.webhook_failed_at)
    changes: dict[str, object] = {}
    if public_base_url is not None and app.webhook_url != public_base_url + WEBHOOK_PATH:
        changes["webhook_url"] = public_base_url + WEBHOOK_PATH
    if set(app.subscribed_events) != set(SUBSCRIBED_EVENTS):
        changes["subscribed_events"] = list(SUBSCRIBED_EVENTS)
    if "refresh_token" not in app.grant_types:
        changes["grant_types"] = sorted({*app.grant_types, "refresh_token"})
    if not changes:
        return []
    await vc.update_app(changes)
    log.info("VirtualCrypto app registration updated", extra={"fields": sorted(changes)})
    app = await vc.get_app()
    async with engine.begin() as conn:
        await webhooks.save_app_state(conn, public_key=app.public_key, webhook_url=app.webhook_url, webhook_verified_at=app.webhook_verified_at, webhook_failed_at=app.webhook_failed_at)
    return sorted(changes)


@dataclass
class Worker:
    settings: Settings
    engine: AsyncEngine
    vc: VCClient
    discord: DiscordRest
    box: SecretBox
    clock: Callable[[], datetime] = utcnow
    _refresh_locks: dict[int, asyncio.Lock] = field(default_factory=dict)

    # Payouts.

    async def payouts_tick(self) -> None:
        for _ in range(MAX_PER_TICK):
            async with self.engine.begin() as conn:
                work = await payouts.take_due(conn, self.clock())
            if not work:
                return
            await self.send_payouts(work)

    async def send_payouts(self, work: list[payouts.Payout]) -> None:
        """Send one leased unit of work and record its outcome; only a refusal from VirtualCrypto compensates."""
        first = work[0]
        ids = [p.id for p in work]
        try:
            if first.method == "issue":
                await self._issue(first)
            else:
                key = str(first.batch_id if first.batch_id is not None else first.id)
                await self.vc.pay([Payment(receiver_id=p.receiver_id, unit=p.unit, amount=p.amount) for p in work], key)
        except VCError as exc:
            reason = str(exc)
            async with self.engine.begin() as conn:
                if not exc.definitive:
                    await payouts.mark_retry(conn, ids, reason, self.clock())
                elif first.batch_id is not None and len(work) > 1:
                    await payouts.unbatch(conn, first.batch_id, reason)
                else:
                    for payout_id in ids:
                        await payouts.mark_failed(conn, payout_id, reason)
            log.warning("payout not completed", extra={"payouts": [str(i) for i in ids], "definitive": exc.definitive, "reason": reason})
            return
        async with self.engine.begin() as conn:
            await payouts.mark_done(conn, ids)
        log.info("payout completed", extra={"payouts": [str(i) for i in ids], "purpose": first.purpose, "unit": first.unit, "amount": sum(p.amount for p in work)})

    async def _issue(self, payout: payouts.Payout) -> None:
        assert payout.guild_id is not None
        token = await self.guild_token(payout.guild_id)
        try:
            await self.vc.issue(token, payout.receiver_id, payout.amount, str(payout.id))
        except VCRequestError as exc:
            if exc.code != "grant_revoked":
                raise
            # An expired access token is refused the same way as a revoked grant, so the token is renewed once before giving up on the grant.
            token = await self.guild_token(payout.guild_id, force_refresh=True)
            try:
                await self.vc.issue(token, payout.receiver_id, payout.amount, str(payout.id))
            except VCRequestError as retry_exc:
                if retry_exc.code == "grant_revoked":
                    await self._revoke_grant(payout.guild_id, "a freshly refreshed guild token was refused")
                raise

    async def guild_token(self, guild_id: int, *, force_refresh: bool = False) -> str:
        """A usable access token for the guild's grant, refreshed when it is near expiry; VCRequestError when the guild has no usable grant."""
        lock = self._refresh_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            async with self.engine.connect() as conn:
                grant = await grants.active_grant(conn, guild_id)
            if grant is None:
                raise VCRequestError("the guild has no active issuing grant", code="no_grant")
            if not force_refresh and grant.access_expires_at - self.clock() > TOKEN_REFRESH_MARGIN:
                return self.box.decrypt(grant.access_token_enc)
            if grant.refresh_token_enc is None:
                if force_refresh:
                    await self._revoke_grant(guild_id, "the guild token was refused and cannot be refreshed")
                    raise VCRequestError("the guild token was refused", code="no_grant")
                return self.box.decrypt(grant.access_token_enc)
            try:
                tokens = await self.vc.refresh_guild_token(self.box.decrypt(grant.refresh_token_enc))
            except VCRequestError:
                await self._revoke_grant(guild_id, "the refresh token was refused")
                raise
            async with self.engine.begin() as conn:
                await grants.store_tokens(
                    conn,
                    guild_id,
                    access_token_enc=self.box.encrypt(tokens.access_token),
                    access_expires_at=self.clock() + timedelta(seconds=tokens.expires_in),
                    refresh_token_enc=self.box.encrypt(tokens.refresh_token) if tokens.refresh_token else grant.refresh_token_enc,
                )
            return tokens.access_token

    async def _revoke_grant(self, guild_id: int, reason: str) -> None:
        async with self.engine.begin() as conn:
            await grants.revoke(conn, guild_id)
        log.warning("guild issuing grant revoked", extra={"guild_id": guild_id, "reason": reason})

    async def refresh_grants_tick(self) -> None:
        async with self.engine.connect() as conn:
            due = await grants.expiring(conn, self.clock() + TOKEN_REFRESH_MARGIN)
        for grant in due:
            try:
                await self.guild_token(grant.guild_id)
            except VCError as exc:
                log.warning("guild token refresh failed", extra={"guild_id": grant.guild_id, "reason": str(exc)})

    # The webhook inbox.

    async def webhooks_tick(self) -> None:
        for _ in range(MAX_PER_TICK):
            async with self.engine.begin() as conn:
                event = await webhooks.take_next(conn)
                if event is None:
                    return
                try:
                    async with conn.begin_nested():
                        await webhooks.apply(conn, event)
                except Exception as exc:
                    log.exception("webhook event could not be applied", extra={"event_id": event.id})
                    if event.attempts + 1 >= WEBHOOK_MAX_ATTEMPTS:
                        await webhooks.mark_processed(conn, event.id, error=f"{type(exc).__name__}: {exc}")
                    else:
                        await webhooks.mark_attempt_failed(conn, event.id, f"{type(exc).__name__}: {exc}")
                        return
                else:
                    await webhooks.mark_processed(conn, event.id)

    # Claims: reconciliation with VirtualCrypto and expiry.

    async def reconcile_tick(self) -> None:
        """Settle operations whose webhook was missed: awaiting ones by reading their claim, creating ones by finding their claim's metadata."""
        now = self.clock()
        async with self.engine.connect() as conn:
            awaiting = await operations.open_operations(conn, "awaiting_claim")
            creating = await operations.open_operations(conn, "creating", older_than=now - CREATING_GRACE)
        for op in awaiting:
            assert op.claim_id is not None
            try:
                claim = await self.vc.get_claim(op.claim_id)
            except VCError as exc:
                log.warning("claim read failed", extra={"claim_id": op.claim_id, "reason": str(exc)})
                continue
            if claim.status != "pending":
                async with self.engine.begin() as conn:
                    await operations.apply_claim(conn, claim.id, claim.status)
        if creating:
            await self._recover_creating(creating, now)

    async def _recover_creating(self, creating: list[operations.Operation], now: datetime) -> None:
        wanted = {op.id: op for op in creating}
        found: dict[uuid.UUID, tuple[int, str]] = {}
        after: int | None = None
        try:
            for _ in range(CLAIM_SCAN_PAGES):
                page = await self.vc.list_claimed(["pending", "approved", "denied", "canceled"], after_id=after)
                for claim in page:
                    hint = webhooks.operation_hint(claim)
                    if hint in wanted:
                        found[hint] = (claim.id, claim.status)
                if not page or len(found) == len(wanted):
                    break
                after = page[-1].id
        except VCError as exc:
            log.warning("claim list failed", extra={"reason": str(exc)})
            return
        for op_id, op in wanted.items():
            async with self.engine.begin() as conn:
                if op_id in found:
                    claim_id, status = found[op_id]
                    await operations.apply_claim(conn, claim_id, status, operation_hint=op_id)
                elif now - op.created_at > CREATING_GIVE_UP:
                    await operations.fail_creation(conn, op_id, "請求を作成できませんでした。")

    async def expire_tick(self) -> None:
        async with self.engine.connect() as conn:
            expired = await operations.expired_awaiting(conn, self.clock())
        for op in expired:
            assert op.claim_id is not None
            try:
                claim = await self.vc.cancel_claim(op.claim_id)
            except VCRequestError:
                # The claim left pending first (approved or denied just now), so its real outcome wins over the expiry.
                try:
                    claim = await self.vc.get_claim(op.claim_id)
                except VCError as exc:
                    log.warning("claim read failed", extra={"claim_id": op.claim_id, "reason": str(exc)})
                    continue
            except VCError as exc:
                log.warning("claim cancel failed", extra={"claim_id": op.claim_id, "reason": str(exc)})
                continue
            async with self.engine.begin() as conn:
                locked = await operations.get(conn, op.id, lock=True)
                if locked is not None:
                    await operations.apply_claim_status(conn, locked, claim.status, expired=True)

    # Discord messages.

    async def messages_tick(self) -> None:
        for _ in range(MAX_PER_TICK):
            now = self.clock()
            async with self.engine.begin() as conn:
                row = (
                    await conn.execute(
                        select(discord_messages)
                        .where(and_(discord_messages.c.status == "pending", discord_messages.c.next_attempt_at <= now))
                        .order_by(discord_messages.c.id)
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                ).first()
                if row is None:
                    return
                if row.kind == "operation":
                    # One delivery renders the operation's current state, which covers every update queued for it so far.
                    await conn.execute(
                        update(discord_messages)
                        .where(and_(discord_messages.c.operation_id == row.operation_id, discord_messages.c.status == "pending", discord_messages.c.id != row.id))
                        .values(status="done")
                    )
                await conn.execute(update(discord_messages).where(discord_messages.c.id == row.id).values(attempts=row.attempts + 1, next_attempt_at=now + timedelta(minutes=2)))
            try:
                if row.kind == "operation":
                    await self._deliver_operation(row.operation_id)
                else:
                    await self._deliver_dm(int(row.user_id), dict(row.payload))
                status, error, next_at = "done", None, now
            except DiscordError as exc:
                give_up = exc.gone or row.attempts + 1 >= MESSAGE_MAX_ATTEMPTS
                status = "failed" if give_up else "pending"
                error = str(exc)
                next_at = now + timedelta(seconds=exc.retry_after or payouts.backoff(row.attempts + 1).total_seconds())
                log.warning("discord message not delivered", extra={"message_id": row.id, "reason": error, "gave_up": give_up})
            async with self.engine.begin() as conn:
                await conn.execute(update(discord_messages).where(discord_messages.c.id == row.id).values(status=status, last_error=error, next_attempt_at=next_at))

    async def _deliver_operation(self, operation_id: uuid.UUID) -> None:
        async with self.engine.connect() as conn:
            op = await operations.get(conn, operation_id)
            shares = await payouts.for_operation(conn, operation_id) if op is not None else []
        if op is None:
            return
        message = render.operation_message(op, shares)
        usable = op.interaction_token_enc is not None and op.interaction_app_id is not None and op.interaction_expires_at is not None
        if usable and op.interaction_expires_at is not None and op.interaction_expires_at - self.clock() > INTERACTION_MARGIN:
            assert op.interaction_app_id is not None and op.interaction_token_enc is not None
            try:
                await self.discord.edit_original_response(op.interaction_app_id, self.box.decrypt(op.interaction_token_enc), message)
                return
            except DiscordError as exc:
                if not exc.gone:
                    raise
        if op.is_final:
            await self.discord.send_dm(op.requester_id, message)

    async def _deliver_dm(self, user_id: int, payload: dict[str, object]) -> None:
        template = payload.get("template")
        if template == TEMPLATE_SEND_RECEIVED:
            async with self.engine.connect() as conn:
                op = await operations.get(conn, uuid.UUID(str(payload["operation_id"])))
            if op is not None:
                await self.discord.send_dm(user_id, render.send_received_message(op))
            return
        guild_id = int(str(payload.get("guild_id", "0")))
        async with self.engine.connect() as conn:
            names = await guilds.bot_guild_map(conn)
        name = names[guild_id].name if guild_id in names else f"サーバー {guild_id}"
        unit = payload.get("unit")
        await self.discord.send_dm(user_id, render.grant_message(str(template), name, str(unit) if unit else None))

    # Guild issuing grant requests.

    async def grant_requests_tick(self) -> None:
        now = self.clock()
        async with self.engine.connect() as conn:
            due = await grants.due_requests(conn, now)
        for request in due:
            if request.expires_at <= now:
                async with self.engine.begin() as conn:
                    await grants.finish_request(conn, request, "failed", "the request expired before it was approved")
                continue
            try:
                tokens = await self.vc.poll_device_token(self.box.decrypt(request.device_code_enc))
            except VCRequestError as exc:
                async with self.engine.begin() as conn:
                    await grants.finish_request(conn, request, "failed", str(exc))
                continue
            except VCError as exc:
                log.warning("device token poll failed", extra={"guild_id": request.guild_id, "reason": str(exc)})
                async with self.engine.begin() as conn:
                    await grants.postpone(conn, request.id, now + GRANT_POLL_INTERVAL)
                continue
            if tokens is None:
                async with self.engine.begin() as conn:
                    await grants.postpone(conn, request.id, now + GRANT_POLL_INTERVAL)
                continue
            currency = await self.vc.currency_by_guild(request.guild_id)
            async with self.engine.begin() as conn:
                if currency is None:
                    await grants.finish_request(conn, request, "failed", "the guild has no VirtualCrypto currency")
                    continue
                await grants.approve_request(
                    conn,
                    request,
                    grant_id=tokens.grant_id,
                    unit=currency.unit,
                    access_token_enc=self.box.encrypt(tokens.access_token),
                    access_expires_at=now + timedelta(seconds=tokens.expires_in),
                    refresh_token_enc=self.box.encrypt(tokens.refresh_token) if tokens.refresh_token else None,
                )
            log.info("guild issuing grant approved", extra={"guild_id": request.guild_id, "unit": currency.unit})

    # The application's VirtualCrypto registration.

    async def vc_sync_tick(self) -> None:
        await sync_vc_app(self.engine, self.vc, self.settings.public_base_url)

    # Backups and pruning.

    async def backup_tick(self) -> None:
        if not backup.is_due(self.settings.backup_dir, self.settings.backup_interval_seconds, self.clock()):
            return
        target = await asyncio.to_thread(backup.take_backup, self.settings)
        pruned = await asyncio.to_thread(backup.prune_backups, self.settings.backup_dir, self.settings.backup_retention)
        log.info("database backup written", extra={"file": target.name, "pruned": len(pruned)})

    async def prune_tick(self) -> None:
        now = self.clock()
        async with self.engine.begin() as conn:
            await webhooks.prune(conn, now - RETENTION)
            await conn.execute(delete(discord_messages).where(and_(discord_messages.c.status != "pending", discord_messages.c.created_at < now - RETENTION)))
            await conn.execute(delete(dashboard_sessions).where(dashboard_sessions.c.expires_at < func.now()))
            await conn.execute(delete(oauth_states).where(oauth_states.c.created_at < now - OAUTH_STATE_LIFETIME))
