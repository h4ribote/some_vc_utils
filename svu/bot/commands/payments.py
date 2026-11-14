"""/rain and /send: the requester pays the bot through a claim, and the bot pays the recipients once the claim is approved."""

from __future__ import annotations

from typing import Any

import discord
from discord import app_commands

from svu import render
from svu.bot.context import context_of
from svu.claims import start_operation
from svu.core import payouts, users
from svu.core.operations import Operation

AMOUNT_MAX = 2**63 - 1
# Discord refuses integer options whose bounds exceed this, so a slash command amount is limited to it.
DISCORD_INTEGER_MAX = 2**53 - 1
# A rain pays at most this many members, which keeps its claim and its payouts bounded.
RAIN_MAX_MEMBERS = 5000
SEND_MESSAGE_MAX = 1000

Amount = app_commands.Range[int, 1, DISCORD_INTEGER_MAX]


async def show_operation(interaction: discord.Interaction[Any], op: Operation) -> None:
    """Show the operation's current state in the interaction's original response."""
    async with context_of(interaction).engine.connect() as conn:
        shares = await payouts.for_operation(conn, op.id)
    await interaction.edit_original_response(**_message(op, shares))


def _message(op: Operation, shares: list[payouts.Payout]) -> dict[str, Any]:
    return {"content": None, "embed": render.operation_embed(op, shares), "allowed_mentions": discord.AllowedMentions.none()}


@app_commands.command(name="rain", description="ロールのメンバー全員に通貨を配ります")
@app_commands.describe(unit="通貨の単位", amount_per_user="1人あたりの数量", role="配布先のロール")
@app_commands.guild_only()
async def rain(interaction: discord.Interaction[Any], unit: str, amount_per_user: Amount, role: discord.Role) -> None:
    ctx = context_of(interaction)
    members = [m.id for m in role.members if not m.bot]
    if not members:
        await interaction.response.send_message(embed=render.error_embed("対象のロールにメンバーがいません。"), ephemeral=True)
        return
    if len(members) > RAIN_MAX_MEMBERS:
        await interaction.response.send_message(embed=render.error_embed(f"一度に配布できるのは{RAIN_MAX_MEMBERS}人までです。"), ephemeral=True)
        return
    if amount_per_user * len(members) > AMOUNT_MAX:
        await interaction.response.send_message(embed=render.error_embed("合計金額が大きすぎます。"), ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    op = await start_operation(
        ctx.engine,
        ctx.vc,
        kind="rain",
        requester_id=interaction.user.id,
        unit=unit,
        recipients=[(member, amount_per_user) for member in members],
        guild_id=interaction.guild_id,
        channel_id=interaction.channel_id,
        details={"role_id": str(role.id), "member_count": len(members), "amount_per_user": amount_per_user},
        interaction=ctx.interaction_ref(interaction),
        timeout_seconds=ctx.settings.claim_timeout_seconds,
    )
    await show_operation(interaction, op)


@app_commands.command(name="send", description="DMでのメッセージと一緒に通貨を送ります")
@app_commands.describe(unit="通貨の単位", user="送り先", amount="数量", message="DMで届けるメッセージ")
async def send(interaction: discord.Interaction[Any], unit: str, user: discord.User, amount: Amount, message: app_commands.Range[str, 1, SEND_MESSAGE_MAX]) -> None:
    ctx = context_of(interaction)
    if user.bot or user.id == interaction.user.id:
        await interaction.response.send_message(embed=render.error_embed("自分自身やBotには送れません。"), ephemeral=True)
        return
    async with ctx.engine.connect() as conn:
        accepts = await users.receives_dm(conn, user.id)
    if not accepts:
        await interaction.response.send_message(embed=render.error_embed(f"{user.mention} はメッセージの受け取りを有効にしていません (`/settings dm`)。"), ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    op = await start_operation(
        ctx.engine,
        ctx.vc,
        kind="send",
        requester_id=interaction.user.id,
        unit=unit,
        recipients=[(user.id, amount)],
        guild_id=interaction.guild_id,
        channel_id=interaction.channel_id,
        details={"receiver_id": str(user.id), "message": message},
        interaction=ctx.interaction_ref(interaction),
        timeout_seconds=ctx.settings.claim_timeout_seconds,
    )
    await show_operation(interaction, op)


COMMANDS: list[app_commands.Command[Any, Any, Any] | app_commands.Group] = [rain, send]
