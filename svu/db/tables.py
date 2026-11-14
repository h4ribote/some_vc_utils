"""Schema of the application database; the Alembic migrations create exactly these tables."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

TZ = TIMESTAMP(timezone=True)

REWARD_TYPES = ("message", "server_join")
FUNDING_SOURCES = ("wallet", "issue")
OPERATION_KINDS = ("rain", "send", "deposit")
OPERATION_STATUSES = ("creating", "awaiting_claim", "paying", "done", "partial", "failed", "denied", "canceled", "expired")
PAYOUT_METHODS = ("pay", "issue")
PAYOUT_PURPOSES = ("rain", "send", "reward", "refund", "withdraw")
PAYOUT_STATUSES = ("held", "pending", "done", "failed", "canceled", "unknown")
POOL_ENTRY_KINDS = ("import", "deposit", "reward", "reward_release", "withdraw", "withdraw_release")
GRANT_STATUSES = ("active", "revoked")
GRANT_REQUEST_STATUSES = ("pending", "approved", "expired", "failed")
MESSAGE_KINDS = ("operation", "dm")
MESSAGE_STATUSES = ("pending", "done", "failed")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _created_at() -> Column[object]:
    return Column("created_at", TZ, nullable=False, server_default=func.now())


users = Table(
    "users",
    metadata,
    Column("discord_id", BigInteger, primary_key=True, autoincrement=False),
    Column("receive_dm", Boolean, nullable=False, server_default=false()),
    Column("updated_at", TZ, nullable=False, server_default=func.now()),
)

bot_guilds = Table(
    "bot_guilds",
    metadata,
    Column("guild_id", BigInteger, primary_key=True, autoincrement=False),
    Column("name", Text, nullable=False),
    Column("icon", Text),
    Column("present", Boolean, nullable=False),
    Column("updated_at", TZ, nullable=False, server_default=func.now()),
)

guild_settings = Table(
    "guild_settings",
    metadata,
    Column("guild_id", BigInteger, primary_key=True, autoincrement=False),
    Column("reward_unit", Text),
    Column("funding_source", Text, nullable=False, server_default="wallet"),
    Column("updated_at", TZ, nullable=False, server_default=func.now()),
    CheckConstraint(_in("funding_source", FUNDING_SOURCES), name="funding_source"),
)

reward_pool_balances = Table(
    "reward_pool_balances",
    metadata,
    Column("guild_id", BigInteger, nullable=False),
    Column("unit", Text, nullable=False),
    Column("balance", BigInteger, nullable=False),
    PrimaryKeyConstraint("guild_id", "unit"),
    CheckConstraint("balance >= 0", name="balance_not_negative"),
)

reward_pool_entries = Table(
    "reward_pool_entries",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("guild_id", BigInteger, nullable=False),
    Column("unit", Text, nullable=False),
    Column("delta", BigInteger, nullable=False),
    Column("kind", Text, nullable=False),
    Column("operation_id", UUID(as_uuid=True)),
    Column("payout_id", UUID(as_uuid=True)),
    Column("actor_id", BigInteger),
    _created_at(),
    CheckConstraint(_in("kind", POOL_ENTRY_KINDS), name="kind"),
    Index(None, "guild_id", "id"),
)

reward_rules = Table(
    "reward_rules",
    metadata,
    Column("guild_id", BigInteger, nullable=False),
    Column("reward_type", Text, nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("cooldown_seconds", Integer, nullable=False),
    Column("updated_at", TZ, nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("guild_id", "reward_type"),
    CheckConstraint(_in("reward_type", REWARD_TYPES), name="reward_type"),
    CheckConstraint("amount > 0", name="amount_positive"),
    CheckConstraint("cooldown_seconds >= 0 AND cooldown_seconds <= 31536000", name="cooldown_range"),
)

reward_cooldowns = Table(
    "reward_cooldowns",
    metadata,
    Column("guild_id", BigInteger, nullable=False),
    Column("user_id", BigInteger, nullable=False),
    Column("reward_type", Text, nullable=False),
    Column("last_rewarded_at", TZ, nullable=False),
    PrimaryKeyConstraint("guild_id", "user_id", "reward_type"),
)

operations = Table(
    "operations",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("guild_id", BigInteger),
    Column("channel_id", BigInteger),
    Column("requester_id", BigInteger, nullable=False),
    Column("unit", Text, nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("claim_id", BigInteger, unique=True),
    Column("details", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("interaction_app_id", BigInteger),
    Column("interaction_token_enc", Text),
    Column("interaction_expires_at", TZ),
    _created_at(),
    Column("expires_at", TZ, nullable=False),
    Column("resolved_at", TZ),
    Column("error", Text),
    CheckConstraint(_in("kind", OPERATION_KINDS), name="kind"),
    CheckConstraint(_in("status", OPERATION_STATUSES), name="status"),
    CheckConstraint("amount > 0", name="amount_positive"),
    Index(None, "status"),
    Index(None, "guild_id", "created_at"),
    Index(None, "requester_id", "created_at"),
)

payouts = Table(
    "payouts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("operation_id", UUID(as_uuid=True), ForeignKey("operations.id")),
    Column("refund_of", UUID(as_uuid=True), ForeignKey("payouts.id")),
    Column("guild_id", BigInteger),
    Column("receiver_id", BigInteger, nullable=False),
    Column("unit", Text, nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("method", Text, nullable=False),
    Column("purpose", Text, nullable=False),
    Column("reward_type", Text),
    Column("cooldown_at", TZ),
    Column("cooldown_prev", TZ),
    Column("batch_id", UUID(as_uuid=True)),
    Column("status", Text, nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", Text),
    Column("next_attempt_at", TZ, nullable=False, server_default=func.now()),
    _created_at(),
    Column("completed_at", TZ),
    CheckConstraint(_in("method", PAYOUT_METHODS), name="method"),
    CheckConstraint(_in("purpose", PAYOUT_PURPOSES), name="purpose"),
    CheckConstraint(_in("status", PAYOUT_STATUSES), name="status"),
    CheckConstraint("amount > 0", name="amount_positive"),
    CheckConstraint("method = 'pay' OR guild_id IS NOT NULL", name="issue_has_guild"),
    Index(None, "status", "next_attempt_at"),
    Index(None, "operation_id"),
    Index(None, "guild_id", "created_at"),
)

issue_grants = Table(
    "issue_grants",
    metadata,
    Column("guild_id", BigInteger, primary_key=True, autoincrement=False),
    Column("grant_id", Text),
    Column("unit", Text, nullable=False),
    Column("access_token_enc", Text, nullable=False),
    Column("access_expires_at", TZ, nullable=False),
    Column("refresh_token_enc", Text),
    Column("status", Text, nullable=False),
    Column("updated_at", TZ, nullable=False, server_default=func.now()),
    CheckConstraint(_in("status", GRANT_STATUSES), name="status"),
)

grant_requests = Table(
    "grant_requests",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("guild_id", BigInteger, nullable=False),
    Column("requested_by", BigInteger, nullable=False),
    Column("device_code_enc", Text, nullable=False),
    Column("user_code", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("expires_at", TZ, nullable=False),
    Column("next_poll_at", TZ, nullable=False, server_default=func.now()),
    _created_at(),
    Column("completed_at", TZ),
    Column("error", Text),
    CheckConstraint(_in("status", GRANT_REQUEST_STATUSES), name="status"),
    Index(None, "status", "next_poll_at"),
)

webhook_events = Table(
    "webhook_events",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("received_at", TZ, nullable=False, server_default=func.now()),
    Column("type", Integer, nullable=False),
    Column("body", JSONB, nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("processed_at", TZ),
    Column("error", Text),
    Index("ix_webhook_events_unprocessed", "id", postgresql_where=text("processed_at IS NULL")),
)

vc_app = Table(
    "vc_app",
    metadata,
    Column("id", SmallInteger, primary_key=True, autoincrement=False),
    Column("public_key", Text, nullable=False),
    Column("webhook_url", Text),
    Column("webhook_verified_at", TZ),
    Column("webhook_failed_at", TZ),
    Column("synced_at", TZ, nullable=False),
    CheckConstraint("id = 1", name="singleton"),
)

discord_messages = Table(
    "discord_messages",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("operation_id", UUID(as_uuid=True), ForeignKey("operations.id")),
    Column("user_id", BigInteger),
    Column("payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", Text),
    Column("next_attempt_at", TZ, nullable=False, server_default=func.now()),
    _created_at(),
    CheckConstraint(_in("kind", MESSAGE_KINDS), name="kind"),
    CheckConstraint(_in("status", MESSAGE_STATUSES), name="status"),
    Index(None, "status", "next_attempt_at"),
)

dashboard_sessions = Table(
    "dashboard_sessions",
    metadata,
    Column("token_digest", Text, primary_key=True),
    Column("user_id", BigInteger, nullable=False),
    Column("username", Text, nullable=False),
    Column("avatar", Text),
    Column("discord_token_enc", Text, nullable=False),
    Column("guilds", JSONB),
    Column("guilds_fetched_at", TZ),
    Column("csrf_token", Text, nullable=False),
    Column("flash", JSONB),
    _created_at(),
    Column("expires_at", TZ, nullable=False),
)

oauth_states = Table(
    "oauth_states",
    metadata,
    Column("state_digest", Text, primary_key=True),
    Column("next_path", Text, nullable=False),
    _created_at(),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    _created_at(),
    Column("actor_id", BigInteger),
    Column("guild_id", BigInteger),
    Column("action", Text, nullable=False),
    Column("detail", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Index(None, "guild_id", "id"),
)
