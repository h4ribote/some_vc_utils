"""/info, /settings and /rewards: commands anyone can use."""

from __future__ import annotations

from typing import Any

import discord
from discord import app_commands

from svu import render
from svu.bot.context import context_of
from svu.core import grants, guilds, users

REWARD_LABELS = {"message": "メッセージの送信", "server_join": "サーバーへの参加"}


def bot_info(public_base_url: str | None) -> discord.Embed:
    embed = discord.Embed(title="Some VC Utils", description="VirtualCryptoと連携してDiscordサーバーで通貨を便利に使うためのBotです", colour=render.INFO)
    embed.add_field(name="開発者", value="h4ribote", inline=False)
    embed.add_field(name="GitHub", value="https://github.com/h4ribote/some_vc_utils", inline=False)
    embed.add_field(name="サポートサーバー", value="https://discord.gg/rqpSGFSRUH", inline=False)
    if public_base_url:
        embed.add_field(name="ダッシュボード", value=public_base_url, inline=False)
    return embed


def cooldown_text(seconds: int) -> str:
    if seconds == 0:
        return "なし"
    for unit_seconds, label in ((86400, "日"), (3600, "時間"), (60, "分")):
        if seconds % unit_seconds == 0:
            return f"{seconds // unit_seconds}{label}"
    return f"{seconds}秒"


@app_commands.command(name="info", description="このBotの情報を表示します")
async def info(interaction: discord.Interaction[Any]) -> None:
    await interaction.response.send_message(embed=bot_info(context_of(interaction).settings.public_base_url))


settings_group = app_commands.Group(name="settings", description="あなたの設定")


@settings_group.command(name="dm", description="/send で届くメッセージをDMで受け取るかどうかを設定します")
@app_commands.describe(enabled="受け取るならTrue")
async def settings_dm(interaction: discord.Interaction[Any], enabled: bool) -> None:
    async with context_of(interaction).engine.begin() as conn:
        await users.set_receive_dm(conn, interaction.user.id, enabled)
    text = "/send のメッセージをDMで受け取ります。" if enabled else "/send のメッセージを受け取りません。"
    await interaction.response.send_message(embed=render.success_embed(text, "設定しました"), ephemeral=True)


@app_commands.command(name="rewards", description="このサーバーで受け取れる報酬を表示します")
@app_commands.guild_only()
async def rewards(interaction: discord.Interaction[Any]) -> None:
    assert interaction.guild_id is not None
    async with context_of(interaction).engine.connect() as conn:
        settings = await guilds.settings(conn, interaction.guild_id)
        rules = await guilds.rules(conn, interaction.guild_id)
        grant = await grants.active_grant(conn, interaction.guild_id) if settings.funding_source == "issue" else None
    unit = grant.unit if grant else settings.reward_unit
    if not rules or unit is None or (settings.funding_source == "issue" and grant is None):
        await interaction.response.send_message(embed=render.info_embed("このサーバーでは報酬が設定されていません。", "報酬"), ephemeral=True)
        return
    embed = discord.Embed(title="このサーバーの報酬", colour=render.INFO)
    for rule in rules:
        embed.add_field(name=REWARD_LABELS[rule.reward_type], value=f"{render.amount(rule.amount, unit)} (間隔: {cooldown_text(rule.cooldown_seconds)})", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


COMMANDS: list[app_commands.Command[Any, Any, Any] | app_commands.Group] = [info, settings_group, rewards]
