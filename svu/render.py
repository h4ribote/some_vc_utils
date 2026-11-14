"""Discord embeds shared by the bot, which sends an operation's first state, and the worker, which shows every later one."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from discord import Colour, Embed

from svu.core.operations import Operation
from svu.core.payouts import Payout

SUCCESS = Colour.from_str("#38ea41")
ERROR = Colour.from_str("#ea3876")
WARNING = Colour.from_str("#ffa158")
INFO = Colour.from_str("#58bcff")
NEUTRAL = Colour.from_str("#cbcbcb")

KIND_LABELS = {"rain": "エアドロップ", "send": "メッセージ付き送金", "deposit": "報酬プールへの入金"}

# An embed field value holds at most this many characters.
FIELD_LIMIT = 1024


def amount(value: int, unit: str) -> str:
    return f"{value:,} {unit}"


def timestamp(value: datetime, style: str = "f") -> str:
    return f"<t:{int(value.timestamp())}:{style}>"


def mention_list(user_ids: Sequence[int], limit: int = FIELD_LIMIT) -> str:
    """Mentions joined by spaces, ending with a count of those that did not fit in limit characters."""
    parts: list[str] = []
    for index, user_id in enumerate(user_ids):
        remaining = len(user_ids) - index
        candidate = " ".join([*parts, f"<@{user_id}>"])
        reserved = f" ...他{remaining - 1}人" if remaining > 1 else ""
        if len(candidate) + len(reserved) > limit:
            return f"{' '.join(parts)} ...他{remaining}人".strip()
        parts.append(f"<@{user_id}>")
    return " ".join(parts)


def _summary(op: Operation) -> str:
    details = op.details
    if op.kind == "rain":
        return f"<@&{details.get('role_id')}> の {details.get('member_count')}人に 1人あたり **{amount(int(details.get('amount_per_user', 0)), op.unit)}**"
    if op.kind == "send":
        return f"<@{details.get('receiver_id')}> に **{amount(op.amount, op.unit)}**"
    return f"このサーバーの報酬プールに **{amount(op.amount, op.unit)}**"


def operation_embed(op: Operation, payouts: Sequence[Payout] = ()) -> Embed:
    """The embed that shows an operation in its current state, from claim issuance to the payout result."""
    label = KIND_LABELS[op.kind]
    embed = Embed(description=_summary(op))
    if op.claim_id is not None:
        embed.add_field(name="請求ID", value=f"`{op.claim_id}`")
    embed.add_field(name="請求額", value=amount(op.amount, op.unit))
    status = op.status
    if status == "creating":
        embed.title, embed.colour = f"{label}: 請求を作成しています", NEUTRAL
    elif status == "awaiting_claim":
        embed.title, embed.colour = f"{label}: 請求の承認を待っています", INFO
        embed.add_field(name="承認方法", value=f"VirtualCryptoで `/claim approve id:{op.claim_id}` を実行してください。", inline=False)
        embed.add_field(name="期限", value=f"{timestamp(op.expires_at, 'R')}までに承認されない場合、請求は取り消されます。", inline=False)
    elif status == "paying":
        counts = Counter(p.status for p in payouts if p.purpose != "refund")
        embed.title, embed.colour = f"{label}: 支払い中", INFO
        embed.add_field(name="進捗", value=f"{counts.get('done', 0)} / {sum(counts.values())}人", inline=False)
    elif status in ("done", "partial", "failed"):
        _result(embed, op, payouts, label)
    elif status == "denied":
        embed.title, embed.colour = f"{label}: 請求は拒否されました", ERROR
    elif status == "canceled":
        embed.title, embed.colour = f"{label}: 請求は取り消されました", ERROR
    elif status == "expired":
        embed.title, embed.colour = f"{label}: 期限までに承認されなかったため取り消しました", ERROR
    if op.status == "failed" and op.error and not payouts:
        embed.add_field(name="エラー", value=op.error[:FIELD_LIMIT], inline=False)
    return embed


def _result(embed: Embed, op: Operation, payouts: Sequence[Payout], label: str) -> None:
    shares = [p for p in payouts if p.purpose != "refund"]
    refunds = [p for p in payouts if p.purpose == "refund"]
    failed = [p for p in shares if p.status == "failed"]
    unknown = [p for p in shares if p.status == "unknown"]
    if op.status == "done":
        embed.title, embed.colour = f"{label}: 完了しました", SUCCESS
        if op.kind == "rain":
            embed.add_field(name="配布", value=f"{len(shares)}人に配布しました。", inline=False)
        return
    embed.title = f"{label}: 一部の支払いに失敗しました" if op.status == "partial" else f"{label}: 支払いに失敗しました"
    embed.colour = WARNING if op.status == "partial" else ERROR
    if failed:
        embed.add_field(name=f"支払えなかった相手 ({len(failed)}人)", value=mention_list([p.receiver_id for p in failed]), inline=False)
    if unknown:
        embed.add_field(name=f"結果を確認中の相手 ({len(unknown)}人)", value=mention_list([p.receiver_id for p in unknown]), inline=False)
    if refunds:
        refunded = sum(p.amount for p in refunds if p.status == "done")
        pending = sum(p.amount for p in refunds if p.status in ("pending", "unknown"))
        lines = []
        if refunded:
            lines.append(f"{amount(refunded, op.unit)} を返金しました。")
        if pending:
            lines.append(f"{amount(pending, op.unit)} を返金しています。")
        if any(p.status == "failed" for p in refunds):
            lines.append("返金できなかった分があります。Botの管理者にお問い合わせください。")
        embed.add_field(name="返金", value="\n".join(lines) or "-", inline=False)


def operation_message(op: Operation, payouts: Sequence[Payout] = ()) -> dict[str, Any]:
    """The message body for editing an interaction response or sending a DM, with mentions that never ping."""
    return {"content": "", "embeds": [operation_embed(op, payouts).to_dict()], "allowed_mentions": {"parse": []}}


def send_received_message(op: Operation) -> dict[str, Any]:
    embed = Embed(title="VirtualCryptoで通貨を受け取りました", colour=SUCCESS)
    embed.add_field(name="送信者", value=f"<@{op.requester_id}>", inline=False)
    embed.add_field(name="数量", value=amount(op.amount, op.unit), inline=False)
    embed.add_field(name="メッセージ", value=str(op.details.get("message", ""))[:FIELD_LIMIT] or "-", inline=False)
    return {"embeds": [embed.to_dict()]}


def grant_message(template: str, guild_name: str, unit: str | None = None) -> dict[str, Any]:
    if template == "grant_approved":
        embed = Embed(title="発行の許可を受け取りました", colour=SUCCESS, description=f"{guild_name} の報酬は、これから発行枠から **{unit}** で直接支払われます。")
    elif template == "grant_revoked":
        embed = Embed(title="発行の許可が取り消されました", colour=WARNING, description=f"{guild_name} の報酬の支払いは止まっています。`/reward_pool source` で支払い元を設定し直してください。")
    else:
        embed = Embed(
            title="発行の許可を取得できませんでした", colour=ERROR, description=f"{guild_name} の発行許可の申請は承認されないまま失効しました。もう一度 `/reward_pool source` から申請してください。"
        )
    return {"embeds": [embed.to_dict()]}


def error_embed(message: str, title: str = "エラー") -> Embed:
    return Embed(title=title, description=message, colour=ERROR)


def info_embed(message: str, title: str) -> Embed:
    return Embed(title=title, description=message, colour=INFO)


def success_embed(message: str, title: str) -> Embed:
    return Embed(title=title, description=message, colour=SUCCESS)
