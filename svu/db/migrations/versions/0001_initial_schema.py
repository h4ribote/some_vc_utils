"""Initial schema.

Revision ID: 0001
Revises: none
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index(op.f("ix_audit_log_guild_id_id"), "audit_log", ["guild_id", "id"], unique=False)
    op.create_table(
        "bot_guilds",
        sa.Column("guild_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("icon", sa.Text(), nullable=True),
        sa.Column("present", sa.Boolean(), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_bot_guilds")),
    )
    op.create_table(
        "dashboard_sessions",
        sa.Column("token_digest", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("avatar", sa.Text(), nullable=True),
        sa.Column("discord_token_enc", sa.Text(), nullable=False),
        sa.Column("guilds", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("guilds_fetched_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("csrf_token", sa.Text(), nullable=False),
        sa.Column("flash", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("token_digest", name=op.f("pk_dashboard_sessions")),
    )
    op.create_table(
        "grant_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=False),
        sa.Column("device_code_enc", sa.Text(), nullable=False),
        sa.Column("user_code", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("next_poll_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'approved', 'expired', 'failed')", name=op.f("ck_grant_requests_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_grant_requests")),
    )
    op.create_index(op.f("ix_grant_requests_status_next_poll_at"), "grant_requests", ["status", "next_poll_at"], unique=False)
    op.create_table(
        "guild_settings",
        sa.Column("guild_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("reward_unit", sa.Text(), nullable=True),
        sa.Column("funding_source", sa.Text(), server_default="wallet", nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("funding_source IN ('wallet', 'issue')", name=op.f("ck_guild_settings_funding_source")),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_guild_settings")),
    )
    op.create_table(
        "issue_grants",
        sa.Column("guild_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("grant_id", sa.Text(), nullable=True),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("access_token_enc", sa.Text(), nullable=False),
        sa.Column("access_expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("refresh_token_enc", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('active', 'revoked')", name=op.f("ck_issue_grants_status")),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_issue_grants")),
    )
    op.create_table(
        "oauth_states",
        sa.Column("state_digest", sa.Text(), nullable=False),
        sa.Column("next_path", sa.Text(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("state_digest", name=op.f("pk_oauth_states")),
    )
    op.create_table(
        "operations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("requester_id", sa.BigInteger(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("claim_id", sa.BigInteger(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("interaction_app_id", sa.BigInteger(), nullable=True),
        sa.Column("interaction_token_enc", sa.Text(), nullable=True),
        sa.Column("interaction_expires_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("resolved_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("kind IN ('rain', 'send', 'deposit')", name=op.f("ck_operations_kind")),
        sa.CheckConstraint("status IN ('creating', 'awaiting_claim', 'paying', 'done', 'partial', 'failed', 'denied', 'canceled', 'expired')", name=op.f("ck_operations_status")),
        sa.CheckConstraint("amount > 0", name=op.f("ck_operations_amount_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operations")),
        sa.UniqueConstraint("claim_id", name=op.f("uq_operations_claim_id")),
    )
    op.create_index(op.f("ix_operations_guild_id_created_at"), "operations", ["guild_id", "created_at"], unique=False)
    op.create_index(op.f("ix_operations_requester_id_created_at"), "operations", ["requester_id", "created_at"], unique=False)
    op.create_index(op.f("ix_operations_status"), "operations", ["status"], unique=False)
    op.create_table(
        "reward_cooldowns",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("reward_type", sa.Text(), nullable=False),
        sa.Column("last_rewarded_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", "user_id", "reward_type", name=op.f("pk_reward_cooldowns")),
    )
    op.create_table(
        "reward_pool_balances",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("balance", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("balance >= 0", name=op.f("ck_reward_pool_balances_balance_not_negative")),
        sa.PrimaryKeyConstraint("guild_id", "unit", name=op.f("pk_reward_pool_balances")),
    )
    op.create_table(
        "reward_pool_entries",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("delta", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.UUID(), nullable=True),
        sa.Column("payout_id", sa.UUID(), nullable=True),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('import', 'deposit', 'reward', 'reward_release', 'withdraw', 'withdraw_release')", name=op.f("ck_reward_pool_entries_kind")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reward_pool_entries")),
    )
    op.create_index(op.f("ix_reward_pool_entries_guild_id_id"), "reward_pool_entries", ["guild_id", "id"], unique=False)
    op.create_table(
        "reward_rules",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("reward_type", sa.Text(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("reward_type IN ('message', 'server_join')", name=op.f("ck_reward_rules_reward_type")),
        sa.CheckConstraint("amount > 0", name=op.f("ck_reward_rules_amount_positive")),
        sa.CheckConstraint("cooldown_seconds >= 0 AND cooldown_seconds <= 31536000", name=op.f("ck_reward_rules_cooldown_range")),
        sa.PrimaryKeyConstraint("guild_id", "reward_type", name=op.f("pk_reward_rules")),
    )
    op.create_table(
        "users",
        sa.Column("discord_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("receive_dm", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("discord_id", name=op.f("pk_users")),
    )
    op.create_table(
        "vc_app",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("webhook_url", sa.Text(), nullable=True),
        sa.Column("webhook_verified_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("webhook_failed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("synced_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_vc_app_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vc_app")),
    )
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("received_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("type", sa.Integer(), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("processed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_events")),
    )
    op.create_index("ix_webhook_events_unprocessed", "webhook_events", ["id"], unique=False, postgresql_where=sa.text("processed_at IS NULL"))
    op.create_table(
        "discord_messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.UUID(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('operation', 'dm')", name=op.f("ck_discord_messages_kind")),
        sa.CheckConstraint("status IN ('pending', 'done', 'failed')", name=op.f("ck_discord_messages_status")),
        sa.ForeignKeyConstraint(["operation_id"], ["operations.id"], name=op.f("fk_discord_messages_operation_id_operations")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discord_messages")),
    )
    op.create_index(op.f("ix_discord_messages_status_next_attempt_at"), "discord_messages", ["status", "next_attempt_at"], unique=False)
    op.create_table(
        "payouts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("operation_id", sa.UUID(), nullable=True),
        sa.Column("refund_of", sa.UUID(), nullable=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=True),
        sa.Column("receiver_id", sa.BigInteger(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("reward_type", sa.Text(), nullable=True),
        sa.Column("cooldown_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("cooldown_prev", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("batch_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("method = 'pay' OR guild_id IS NOT NULL", name=op.f("ck_payouts_issue_has_guild")),
        sa.CheckConstraint("method IN ('pay', 'issue')", name=op.f("ck_payouts_method")),
        sa.CheckConstraint("purpose IN ('rain', 'send', 'reward', 'refund', 'withdraw')", name=op.f("ck_payouts_purpose")),
        sa.CheckConstraint("status IN ('held', 'pending', 'done', 'failed', 'canceled', 'unknown')", name=op.f("ck_payouts_status")),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payouts_amount_positive")),
        sa.ForeignKeyConstraint(["operation_id"], ["operations.id"], name=op.f("fk_payouts_operation_id_operations")),
        sa.ForeignKeyConstraint(["refund_of"], ["payouts.id"], name=op.f("fk_payouts_refund_of_payouts")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payouts")),
    )
    op.create_index(op.f("ix_payouts_guild_id_created_at"), "payouts", ["guild_id", "created_at"], unique=False)
    op.create_index(op.f("ix_payouts_operation_id"), "payouts", ["operation_id"], unique=False)
    op.create_index(op.f("ix_payouts_status_next_attempt_at"), "payouts", ["status", "next_attempt_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_payouts_status_next_attempt_at"), table_name="payouts")
    op.drop_index(op.f("ix_payouts_operation_id"), table_name="payouts")
    op.drop_index(op.f("ix_payouts_guild_id_created_at"), table_name="payouts")
    op.drop_table("payouts")
    op.drop_index(op.f("ix_discord_messages_status_next_attempt_at"), table_name="discord_messages")
    op.drop_table("discord_messages")
    op.drop_index("ix_webhook_events_unprocessed", table_name="webhook_events", postgresql_where=sa.text("processed_at IS NULL"))
    op.drop_table("webhook_events")
    op.drop_table("vc_app")
    op.drop_table("users")
    op.drop_table("reward_rules")
    op.drop_index(op.f("ix_reward_pool_entries_guild_id_id"), table_name="reward_pool_entries")
    op.drop_table("reward_pool_entries")
    op.drop_table("reward_pool_balances")
    op.drop_table("reward_cooldowns")
    op.drop_index(op.f("ix_operations_status"), table_name="operations")
    op.drop_index(op.f("ix_operations_requester_id_created_at"), table_name="operations")
    op.drop_index(op.f("ix_operations_guild_id_created_at"), table_name="operations")
    op.drop_table("operations")
    op.drop_table("oauth_states")
    op.drop_table("issue_grants")
    op.drop_table("guild_settings")
    op.drop_index(op.f("ix_grant_requests_status_next_poll_at"), table_name="grant_requests")
    op.drop_table("grant_requests")
    op.drop_table("dashboard_sessions")
    op.drop_table("bot_guilds")
    op.drop_index(op.f("ix_audit_log_guild_id_id"), table_name="audit_log")
    op.drop_table("audit_log")
