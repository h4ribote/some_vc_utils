"""/reward_pool: the guild's reward settings, its pool in the bot's wallet and the issuing grant; visible to members who can manage the server."""

from __future__ import annotations

from typing import Any, Literal

import discord
from discord import app_commands

from svu import guild_admin, render
from svu.bot.commands.general import REWARD_LABELS, cooldown_text
from svu.bot.commands.payments import Amount, show_operation
from svu.bot.context import context_of
from svu.claims import start_operation
from svu.core import grants, guilds, pool

reward_pool = app_commands.Group(
    name="reward_pool",
    description="報酬プールと報酬の設定 (サーバー管理者向け)",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)
rule_group = app_commands.Group(name="rule", description="報酬ルール", parent=reward_pool)

RewardTypeChoice = Literal["message", "server_join"]


async def _guard(interaction: discord.Interaction[Any]) -> bool:
    """Re-check Manage Server at run time, since server owners can override a command's default permissions."""
    permissions = interaction.permissions
    if permissions.manage_guild or permissions.administrator:
        return True
    await interaction.response.send_message(embed=render.error_embed("このコマンドにはサーバーの管理権限が必要です。"), ephemeral=True)
    return False


async def _fail(interaction: discord.Interaction[Any], error: guild_admin.AdminError) -> None:
    if interaction.response.is_done():
        await interaction.edit_original_response(embed=render.error_embed(str(error)))
    else:
        await interaction.response.send_message(embed=render.error_embed(str(error)), ephemeral=True)


@reward_pool.command(name="info", description="報酬プールと報酬ルールを表示します")
async def info(interaction: discord.Interaction[Any]) -> None:
    if not await _guard(interaction):
        return
    assert interaction.guild_id is not None
    guild_id = interaction.guild_id
    async with context_of(interaction).engine.connect() as conn:
        settings = await guilds.settings(conn, guild_id)
        balances = await pool.balances(conn, guild_id)
        rules = await guilds.rules(conn, guild_id)
        grant = await grants.active_grant(conn, guild_id)
        pending = await grants.pending_request(conn, guild_id)
    embed = discord.Embed(title="報酬プール", colour=render.INFO)
    if settings.funding_source == "issue":
        source = f"発行枠 ({grant.unit})" if grant else "発行枠 (許可がないため停止中)"
    else:
        source = f"Botのウォレット ({settings.reward_unit})" if settings.reward_unit else "Botのウォレット (通貨が未設定)"
    embed.add_field(name="支払い元", value=source, inline=False)
    embed.add_field(name="プール残高", value="\n".join(render.amount(b.balance, b.unit) for b in balances) or "なし", inline=False)
    if rules:
        embed.add_field(name="報酬ルール", value="\n".join(f"{REWARD_LABELS[r.reward_type]}: {r.amount:,} (間隔: {cooldown_text(r.cooldown_seconds)})" for r in rules), inline=False)
    else:
        embed.add_field(name="報酬ルール", value="なし", inline=False)
    if pending:
        embed.add_field(name="承認待ちの発行許可", value=f"VirtualCryptoで `/grant approve code:{pending.user_code}` ({render.timestamp(pending.expires_at, 'R')}に失効)", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@reward_pool.command(name="unit", description="Botのウォレットから払う報酬の通貨を設定します")
@app_commands.describe(unit="通貨の単位")
async def unit(interaction: discord.Interaction[Any], unit: str) -> None:
    if not await _guard(interaction):
        return
    assert interaction.guild_id is not None
    ctx = context_of(interaction)
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        await guild_admin.set_reward_unit(ctx.engine, ctx.vc, interaction.guild_id, unit, interaction.user.id)
    except guild_admin.AdminError as exc:
        await _fail(interaction, exc)
        return
    await interaction.edit_original_response(embed=render.success_embed(f"報酬の通貨を **{unit}** にしました。", "設定しました"))


@reward_pool.command(name="deposit", description="請求を通じて報酬プールに入金します")
@app_commands.describe(amount="入金する数量", unit="通貨の単位 (省略すると報酬の通貨)")
async def deposit(interaction: discord.Interaction[Any], amount: Amount, unit: str | None = None) -> None:
    if not await _guard(interaction):
        return
    assert interaction.guild_id is not None
    ctx = context_of(interaction)
    if unit is None:
        async with ctx.engine.connect() as conn:
            unit = (await guilds.settings(conn, interaction.guild_id)).reward_unit
        if unit is None:
            await interaction.response.send_message(embed=render.error_embed("先に `/reward_pool unit` で通貨を設定するか、`unit` を指定してください。"), ephemeral=True)
            return
    await interaction.response.defer(thinking=True)
    op = await start_operation(
        ctx.engine,
        ctx.vc,
        kind="deposit",
        requester_id=interaction.user.id,
        unit=unit,
        deposit_amount=amount,
        guild_id=interaction.guild_id,
        channel_id=interaction.channel_id,
        interaction=ctx.interaction_ref(interaction),
        timeout_seconds=ctx.settings.claim_timeout_seconds,
    )
    await show_operation(interaction, op)


@reward_pool.command(name="withdraw", description="報酬プールから自分のウォレットに引き出します")
@app_commands.describe(amount="引き出す数量", unit="通貨の単位")
async def withdraw(interaction: discord.Interaction[Any], amount: Amount, unit: str) -> None:
    if not await _guard(interaction):
        return
    assert interaction.guild_id is not None
    try:
        await guild_admin.withdraw(context_of(interaction).engine, interaction.guild_id, unit, amount, interaction.user.id)
    except guild_admin.AdminError as exc:
        await _fail(interaction, exc)
        return
    await interaction.response.send_message(embed=render.success_embed(f"**{render.amount(amount, unit)}** をあなたのウォレットに送金します。", "引き出しを受け付けました"), ephemeral=True)


@reward_pool.command(name="source", description="報酬の支払い元を切り替えます")
@app_commands.describe(source="wallet: Botのウォレットのプール / issue: サーバーの発行枠から直接発行")
async def source(interaction: discord.Interaction[Any], source: Literal["wallet", "issue"]) -> None:
    if not await _guard(interaction):
        return
    assert interaction.guild_id is not None
    ctx = context_of(interaction)
    if source == "wallet":
        await guild_admin.use_wallet(ctx.engine, interaction.guild_id, interaction.user.id)
        await interaction.response.send_message(embed=render.success_embed("報酬はBotのウォレットのプールから支払われます。", "設定しました"), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        request = await guild_admin.use_issue(ctx.engine, ctx.vc, ctx.box, interaction.guild_id, interaction.user.id)
    except guild_admin.AdminError as exc:
        await _fail(interaction, exc)
        return
    if request.user_code is None:
        await interaction.edit_original_response(embed=render.success_embed(f"報酬は発行枠から **{request.unit}** で支払われます。", "設定しました"))
        return
    assert request.expires_at is not None
    embed = render.info_embed(
        f"このサーバーで、VirtualCryptoの次のコマンドを実行して発行を許可してください。\n```/grant approve code:{request.user_code}```\n"
        f"{render.timestamp(request.expires_at, 'R')}までに承認されると、報酬は発行枠から **{request.unit}** で支払われるようになり、結果をDMでお知らせします。",
        "発行の許可を申請しました",
    )
    await interaction.edit_original_response(embed=embed)


@rule_group.command(name="set", description="報酬ルールを追加または更新します")
@app_commands.describe(reward_type="報酬の対象", amount="1回の報酬", cooldown_seconds="同じ人が次に受け取れるまでの秒数")
async def rule_set(interaction: discord.Interaction[Any], reward_type: RewardTypeChoice, amount: Amount, cooldown_seconds: app_commands.Range[int, 0, guilds.MAX_COOLDOWN_SECONDS]) -> None:
    if not await _guard(interaction):
        return
    assert interaction.guild_id is not None
    ctx = context_of(interaction)
    try:
        await guild_admin.set_rule(ctx.engine, interaction.guild_id, reward_type, amount, cooldown_seconds, interaction.user.id)
    except guild_admin.AdminError as exc:
        await _fail(interaction, exc)
        return
    ctx.rules.set(interaction.guild_id, reward_type, True)
    text = f"{REWARD_LABELS[reward_type]}: {amount:,} (間隔: {cooldown_text(cooldown_seconds)})"
    await interaction.response.send_message(embed=render.success_embed(text, "報酬ルールを保存しました"), ephemeral=True)


@rule_group.command(name="delete", description="報酬ルールを削除します")
@app_commands.describe(reward_type="報酬の対象")
async def rule_delete(interaction: discord.Interaction[Any], reward_type: RewardTypeChoice) -> None:
    if not await _guard(interaction):
        return
    assert interaction.guild_id is not None
    ctx = context_of(interaction)
    deleted = await guild_admin.delete_rule(ctx.engine, interaction.guild_id, reward_type, interaction.user.id)
    ctx.rules.set(interaction.guild_id, reward_type, False)
    if deleted:
        await interaction.response.send_message(embed=render.success_embed(f"{REWARD_LABELS[reward_type]} の報酬を削除しました。", "削除しました"), ephemeral=True)
    else:
        await interaction.response.send_message(embed=render.error_embed(f"{REWARD_LABELS[reward_type]} の報酬は設定されていません。"), ephemeral=True)


COMMANDS: list[app_commands.Command[Any, Any, Any] | app_commands.Group] = [reward_pool]
