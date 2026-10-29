import discord
from discord import app_commands, Embed, Role, Member, Interaction
from virtualcrypto import AsyncVirtualCryptoClient, Scope, ClaimStatus, Claim
import embedColour
import asyncio
import logging
from dataclasses import dataclass, field
from time import time
from typing import Iterable, Optional

from db_utils import Database
from db_structs import RewardType

log = logging.getLogger(__name__)

# Shared state set by configure() at startup and cleared at shutdown.
_vc_client: Optional[AsyncVirtualCryptoClient] = None
_admin_ids: frozenset[int] = frozenset()


def new_vc_client(client_id: str, client_secret: str) -> AsyncVirtualCryptoClient:
    return AsyncVirtualCryptoClient(client_id=client_id, client_secret=client_secret, scopes=[Scope.Pay, Scope.Claim])


def configure(vc_client: Optional[AsyncVirtualCryptoClient], admin_ids: Iterable[int]) -> None:
    global _vc_client, _admin_ids
    _vc_client = vc_client
    _admin_ids = frozenset(admin_ids)


def get_vc_client() -> AsyncVirtualCryptoClient:
    """Return the VirtualCrypto client shared for the bot's lifetime."""
    if _vc_client is None:
        raise RuntimeError("VirtualCrypto client is not configured")
    return _vc_client


def is_admin_id(user_id: int) -> bool:
    return user_id in _admin_ids

# --- Bot Information ---

def bot_info() -> Embed:
    embed = Embed(title="Some VC Utils", description="VirtualCryptoと連携してDiscordサーバーで通貨を便利に使うためのBotです", color=embedColour.LightBlue)
    embed.add_field(name="開発者", value="h4ribote", inline=False)
    embed.add_field(name="GitHub", value="https://github.com/h4ribote/some_vc_utils", inline=False)
    embed.add_field(name="サポートサーバー", value="https://discord.gg/rqpSGFSRUH", inline=False)
    return embed

# --- Helper Functions ---

async def create_claim_embed(vc_client:AsyncVirtualCryptoClient, payer_id:int, unit:str, amount:int, claim_info:str, expire_min:int = 2) -> tuple[Embed, Claim]:
    """
    請求用のEmbedとClaimオブジェクトを作成します。
    """
    new_claim = await vc_client.create_claim(payer_id, unit, amount)
    claim_embed = Embed(title="請求を発行しました", colour=embedColour.LightBlue)
    claim_embed.description = "下記の通り請求を発行しました\nVirtualCryptoから承認してください"
    claim_embed.add_field(name="請求id", value=f"{new_claim.id}")
    claim_embed.add_field(name="数量", value=f"{amount} {unit}")
    claim_embed.add_field(name="内容", value=claim_info, inline=False)
    claim_embed.add_field(name="承認用コマンド", value=f"```/claim approve id:{new_claim.id}```", inline=False)
    claim_embed.set_footer(text=f"注意: 請求は最大{expire_min}分間有効です")
    return claim_embed, new_claim

async def wait_for_claim_approval(interaction: Interaction, vc_client: AsyncVirtualCryptoClient, claim: Claim, original_embed: Embed) -> bool:
    for i in range(12): # 10sec * 12 = 2min
        await asyncio.sleep(10)
        updated_claim = await vc_client.get_claim(claim.id)
        if updated_claim.status == ClaimStatus.Approved:
            return True
        if updated_claim.status in [ClaimStatus.Denied, ClaimStatus.Canceled]:
            cancel_embed = Embed(description="請求はキャンセルまたは拒否されました", colour=embedColour.Error)
            await interaction.edit_original_response(embeds=[original_embed, cancel_embed])
            return False

    # Timeout
    await vc_client.update_claim(claim.id, ClaimStatus.Canceled)
    timeout_embed = Embed(description="操作はタイムアウトしました", colour=embedColour.Error)
    await interaction.edit_original_response(embeds=[original_embed, timeout_embed])
    return False

@dataclass
class PaymentBatch:
    paid: list = field(default_factory=list)
    failed: list = field(default_factory=list)  # (member, exception) pairs


async def pay_members(vc_client: AsyncVirtualCryptoClient, unit: str, amount: int, members: Iterable[Member]) -> PaymentBatch:
    """Pay each member in turn; a failed payment is recorded and the remaining members are still paid."""
    batch = PaymentBatch()
    for mem in members:
        try:
            await vc_client.pay(unit, mem.id, amount)
            batch.paid.append(mem)
        except Exception as e:
            log.warning("Payment of %s %s to user %s failed: %s: %s", amount, unit, mem.id, type(e).__name__, e)
            batch.failed.append((mem, e))
    return batch


def _mention_list(members: list, limit: int = 1024) -> str:
    """Join member mentions into one embed field value, ending with a count of the members that did not fit."""
    parts: list[str] = []
    for i, mem in enumerate(members):
        remaining = len(members) - i
        candidate = " ".join(parts + [mem.mention])
        reserved = f" ...他{remaining - 1}人" if remaining > 1 else ""
        if len(candidate) + len(reserved) > limit:
            return f"{' '.join(parts)} ...他{remaining}人".strip()
        parts.append(mem.mention)
    return " ".join(parts)


def rain_result_embed(claim_id: int, unit: str, amount_per_user: int, member_count: int, batch: PaymentBatch) -> Embed:
    if not batch.failed:
        confirm_embed = Embed(title="処理が完了しました", colour=embedColour.Success)
        confirm_embed.description = f"請求`{claim_id}`は承認され、{member_count}人のメンバーに **{amount_per_user} {unit}** を配布しました。"
        return confirm_embed
    failed_members = [mem for mem, _ in batch.failed]
    embed = Embed(title="一部のメンバーへの配布に失敗しました", colour=embedColour.Orange if batch.paid else embedColour.Error)
    embed.description = (
        f"請求`{claim_id}`は承認され、{member_count}人中{len(batch.paid)}人のメンバーに **{amount_per_user} {unit}** を配布しました。\n"
        f"{len(failed_members)}人への配布(合計 **{amount_per_user * len(failed_members)} {unit}**)に失敗しました。Botの管理者にお問い合わせください。"
    )
    embed.add_field(name="配布に失敗したメンバー", value=_mention_list(failed_members), inline=False)
    return embed

# --- Core Commands ---

@app_commands.command(name="rain",description="通貨を特定のロールのメンバーに配ります")
@app_commands.describe(unit="通貨の単位", amount_per_user="1ユーザーあたりの数量", role="ロール")
async def rain(interaction:Interaction, unit:str, amount_per_user:int, role:Role):
    await interaction.response.defer(thinking=True)
    try:
        role_mems = role.members
        member_count = len(role_mems)
        if member_count == 0:
            await interaction.edit_original_response(embed=Embed(title="エラー", description="対象ロールにメンバーがいません。", colour=embedColour.Error))
            return

        vc_client = get_vc_client()
        total_amount = amount_per_user * member_count
        claim_embed, new_claim = await create_claim_embed(vc_client, interaction.user.id, unit, total_amount, f"通貨のエアドロップ({amount_per_user} * {member_count})")
        await interaction.followup.send(embeds=[claim_embed])

        if not await wait_for_claim_approval(interaction, vc_client, new_claim, claim_embed):
            return

        batch = await pay_members(vc_client, unit, amount_per_user, role_mems)
        if batch.failed:
            log.error(
                "rain claim %s by user %s: %d of %d payments of %s %s failed: %s",
                new_claim.id, interaction.user.id, len(batch.failed), member_count, amount_per_user, unit,
                ", ".join(f"{mem.id} ({type(e).__name__}: {e})" for mem, e in batch.failed)
            )
        else:
            log.info("rain claim %s by user %s: paid %s %s to %d members", new_claim.id, interaction.user.id, amount_per_user, unit, member_count)

        await interaction.edit_original_response(embeds=[claim_embed, rain_result_embed(new_claim.id, unit, amount_per_user, member_count, batch)])

    except Exception as e:
        log.exception("rain failed")
        await interaction.edit_original_response(embed=Embed(title="内部エラー", description=f"{e.__class__.__name__}:\n{e}", colour=embedColour.Error))

@app_commands.command(name="send_with_msg",description="DMでのメッセージと一緒に通貨を送信します")
@app_commands.describe(unit="通貨単位", user="対象ユーザー", amount="数量", message="メッセージ")
async def send_with_msg(interaction:Interaction, unit:str, user:Member, amount:int, message:str):
    await interaction.response.defer(thinking=True)
    try:
        with Database() as db:
            if not db.get_receive_msg_user(user.id):
                await interaction.edit_original_response(embed=Embed(title="エラー", description="対象のユーザーは`/receive_msg`が無効に設定されています", colour=embedColour.Error))
                return

        vc_client = get_vc_client()
        claim_embed, new_claim = await create_claim_embed(vc_client, interaction.user.id, unit, amount, "`/send_with_msg`による送信")
        await interaction.followup.send(embeds=[claim_embed])

        if not await wait_for_claim_approval(interaction, vc_client, new_claim, claim_embed):
            return

        await vc_client.pay(unit, user.id, amount)
        dm_channel = await user.create_dm()
        dm_embed = Embed(title="VirtualCryptoで通貨を受け取りました", colour=embedColour.Green)
        dm_embed.add_field(name="送信者", value=interaction.user.mention, inline=False)
        dm_embed.add_field(name="数量", value=f"{amount} {unit}", inline=False)
        dm_embed.add_field(name="メッセージ", value=message, inline=False)
        await dm_channel.send(embed=dm_embed)

        confirm_embed = Embed(title="処理が完了しました", colour=embedColour.Success)
        confirm_embed.description = f"請求`{new_claim.id}`は承認され、正常に処理されました"
        await interaction.edit_original_response(embeds=[claim_embed, confirm_embed])

    except Exception as e:
        log.exception("send_with_msg failed")
        await interaction.edit_original_response(embed=Embed(title="内部エラー", description=f"{e.__class__.__name__}:\n{e}", colour=embedColour.Error))

@app_commands.command(name="receive_msg",description="/send_with_msgの内容をDMで通知します")
@app_commands.describe(receive_config="[True]DMを受け取る [False]DMを受け取らない")
async def receive_msg(interaction:Interaction, receive_config:bool):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        with Database() as db:
            if receive_config:
                db.add_receive_msg_user(interaction.user.id)
                await interaction.followup.send(embed=Embed(title="設定完了", description="/send_with_msgの内容がDMで通知されます", colour=embedColour.LightBlue))
            else:
                db.remove_receive_msg_user(interaction.user.id)
                await interaction.followup.send(embed=Embed(title="設定完了", description="/send_with_msgの内容はDMで通知されません", colour=embedColour.LightBlue))
    except Exception as e:
        log.exception("receive_msg failed")
        await interaction.edit_original_response(embed=Embed(title="内部エラー", description=f"{type(e).__name__}:\n{e}", colour=embedColour.Error))

# --- Reward Pool Commands ---

reward_pool = app_commands.Group(name="reward_pool", description="サーバー内の報酬に関する設定")

@reward_pool.command(name="init", description="このサーバーの報酬プールの通貨単位を初期設定します (管理者向け)")
@app_commands.describe(unit="報酬として使用する通貨の単位")
@app_commands.checks.has_permissions(manage_guild=True)
async def reward_pool_init(interaction: Interaction, unit: str):
    await interaction.response.defer(thinking=True)
    try:
        # The old balance is taken out of the pool before it is refunded, so no reward can be paid from it meanwhile.
        with Database() as db:
            change = db.change_reward_pool_unit(interaction.guild_id, unit)

        if change:
            try:
                await get_vc_client().pay(change.old_unit, interaction.user.id, change.old_balance)
            except Exception as e:
                log.exception("Refund of %s %s from the reward pool of guild %s failed", change.old_balance, change.old_unit, change.guild_id)
                reverted = False
                try:
                    with Database() as db:
                        reverted = db.revert_reward_pool_unit(change)
                except Exception:
                    log.exception("Reverting the reward pool unit change of guild %s failed", change.guild_id)
                if reverted:
                    note = f"通貨単位の変更は取り消され、プール残高 **{change.old_balance} {change.old_unit}** は元に戻りました。"
                else:
                    log.error("Reward pool of guild %s lost %s %s that were not refunded; manual correction required", change.guild_id, change.old_balance, change.old_unit)
                    note = "通貨単位の変更を取り消せませんでした。Botの管理者にお問い合わせください。"
                await interaction.edit_original_response(content=None, embed=Embed(title="内部エラー", description=f"{type(e).__name__}:\n{e}\n{note}", colour=embedColour.Error), view=None)
                return
            log.info("Reward pool of guild %s switched from %s to %s; refunded %s %s to user %s", change.guild_id, change.old_unit, unit, change.old_balance, change.old_unit, interaction.user.id)
            success_embed = Embed(
                title="処理完了",
                description=f"プール残高 **{change.old_balance} {change.old_unit}** をあなたのウォレットに返金し、通貨単位を **{unit}** に更新しました。",
                color=embedColour.Success
            )
            await interaction.followup.send(embed=success_embed)
            return

        await interaction.followup.send(embed=Embed(title="設定完了", description=f"報酬プールの通貨単位を **{unit}** に設定しました。", colour=embedColour.Success))
    except Exception as e:
        log.exception("reward_pool init failed")
        await interaction.edit_original_response(content=None, embed=Embed(title="内部エラー", description=f"{type(e).__name__}:\n{e}", colour=embedColour.Error), view=None)

@reward_pool.command(name="set", description="報酬ルールを追加または更新します (管理者向け)")
@app_commands.describe(reward_type="報酬の種類 (例: message, server_joinなど)", amount="報酬量", cooldown_seconds="次の報酬までの待機時間(秒)")
@app_commands.checks.has_permissions(manage_guild=True)
async def reward_pool_set(interaction: Interaction, reward_type: RewardType, amount: int, cooldown_seconds: int):
    await interaction.response.defer(thinking=True)
    if amount <= 0 or cooldown_seconds < 0:
        await interaction.followup.send(embed=Embed(title="エラー", description="報酬量とクールダウンは0以上の値を設定してください。", colour=embedColour.Error))
        return
    if len(reward_type) > 20 or cooldown_seconds > 31557600:
        await interaction.followup.send(embed=Embed(title="エラー", description="無効な値です", colour=embedColour.Error))
        return

    try:
        with Database() as db:
            if not db.get_reward_pool(interaction.guild_id):
                await interaction.followup.send(embed=Embed(title="エラー", description="先に`/reward_pool init`で通貨単位を設定してください。", colour=embedColour.Error))
                return

            db.set_reward_config(interaction.guild_id, reward_type.lower(), amount, cooldown_seconds)
            embed = Embed(title="設定完了", description=f"報酬ルール **{reward_type.lower()}** を保存しました。", colour=embedColour.Success)
            embed.add_field(name="報酬量", value=str(amount))
            embed.add_field(name="クールダウン", value=f"{cooldown_seconds}秒")
            await interaction.followup.send(embed=embed)
    except Exception as e:
        log.exception("reward_pool set failed")
        await interaction.followup.send(embed=Embed(title="内部エラー", description=f"{type(e).__name__}:\n{e}", colour=embedColour.Error))

@reward_pool.command(name="delete", description="報酬ルールを削除します (管理者向け)")
@app_commands.describe(reward_type="削除する報酬の種類 (例: message)")
@app_commands.checks.has_permissions(manage_guild=True)
async def reward_pool_delete(interaction: Interaction, reward_type: RewardType):
    await interaction.response.defer(thinking=True)
    try:
        with Database() as db:
            if db.delete_reward_config(interaction.guild_id, reward_type.lower()) > 0:
                await interaction.followup.send(embed=Embed(title="削除完了", description=f"報酬ルール **{reward_type.lower()}** を削除しました。", colour=embedColour.Success))
            else:
                await interaction.followup.send(embed=Embed(title="エラー", description=f"報酬ルール **{reward_type.lower()}** は見つかりませんでした。", colour=embedColour.Error))
    except Exception as e:
        log.exception("reward_pool delete failed")
        await interaction.followup.send(embed=Embed(title="内部エラー", description=f"{type(e).__name__}:\n{e}", colour=embedColour.Error))

@reward_pool.command(name="info", description="報酬プールの現在の情報を表示します")
async def reward_pool_info(interaction: Interaction):
    await interaction.response.defer(thinking=True)
    try:
        with Database() as db:
            pool = db.get_reward_pool(interaction.guild_id)

            if not pool:
                await interaction.followup.send(embed=Embed(title="情報", description="このサーバーでは報酬プールがまだ設定されていません。", colour=embedColour.Yellow))
                return

            embed = Embed(title="報酬プール情報", description=f"{interaction.guild.name}の現在の設定です。", colour=embedColour.LightBlue)
            embed.add_field(name="プール残高", value=f"{pool.pool_balance} {pool.unit}", inline=False)

            configs = db.get_reward_configs(interaction.guild_id)

            if not configs:
                embed.add_field(name="報酬ルール", value="まだ設定されていません。", inline=False)
            else:
                config_text = ""
                for config in configs:
                    config_text += f"**タイプ: `{config.reward_type}`**\n"
                    config_text += f"- 報酬: `{config.amount} {pool.unit}`\n"
                    config_text += f"- クールダウン: `{config.cooldown_seconds}`秒\n"
                embed.add_field(name="報酬ルール一覧", value=config_text, inline=False)

            await interaction.followup.send(embed=embed)
    except Exception as e:
        log.exception("reward_pool info failed")
        await interaction.followup.send(embed=Embed(title="内部エラー", description=f"{type(e).__name__}:\n{e}", colour=embedColour.Error))

@reward_pool.command(name="deposit", description="報酬プールに通貨を補充します (管理者向け)")
@app_commands.describe(amount="補充する数量")
@app_commands.checks.has_permissions(manage_guild=True)
async def reward_pool_deposit(interaction: Interaction, amount: int):
    await interaction.response.defer(thinking=True)
    try:
        with Database() as db:
            pool = db.get_reward_pool(interaction.guild_id)

            if not pool:
                await interaction.edit_original_response(embed=Embed(title="エラー", description="先に`/reward_pool init`で報酬プールを設定してください。", colour=embedColour.Error))
                return
            if amount <= 0:
                await interaction.edit_original_response(embed=Embed(title="エラー", description="補充する数量は0より大きい値を設定してください。", colour=embedColour.Error))
                return

            initial_unit = pool.unit

        vc_client = get_vc_client()
        claim_embed, new_claim = await create_claim_embed(vc_client, interaction.user.id, initial_unit, amount, "報酬プールへの補充")
        await interaction.followup.send(embeds=[claim_embed])

        if not await wait_for_claim_approval(interaction, vc_client, new_claim, claim_embed):
            return

        # The approved amount is already in the bot's wallet, so anything that keeps it out of the pool refunds it.
        deposit_error = None
        try:
            with Database() as db:
                deposited, current_unit = db.deposit_to_reward_pool_if_unit(interaction.guild_id, initial_unit, amount)
        except Exception as e:
            log.exception("Deposit of %s %s into the reward pool of guild %s failed; refunding", amount, initial_unit, interaction.guild_id)
            deposited, current_unit, deposit_error = False, None, e

        if not deposited:
            if deposit_error is None:
                reason = f"入金処理中に報酬プールの通貨単位が **{initial_unit}** から **{current_unit}** に変更されました。\n"
            else:
                reason = "入金処理中にエラーが発生しました。\n"
            refund_embed = Embed(
                title="処理中断と返金",
                description=reason + f"承認された **{amount} {initial_unit}** はプールに入金せず、あなたのウォレットに返金します。",
                color=embedColour.Orange
            )
            if deposit_error is not None:
                refund_embed.add_field(name="エラー詳細", value=f"{type(deposit_error).__name__}: {deposit_error}", inline=False)
            try:
                await vc_client.pay(initial_unit, interaction.user.id, amount)
                refund_embed.color = embedColour.Success
                refund_embed.title = "処理中断と返金完了"
                log.info("Refunded %s %s to user %s instead of depositing into guild %s", amount, initial_unit, interaction.user.id, interaction.guild_id)
            except Exception as e:
                log.exception("Refund of %s %s to user %s failed; manual refund required", amount, initial_unit, interaction.user.id)
                refund_embed.color = embedColour.Error
                refund_embed.title = "返金エラー"
                refund_embed.add_field(name="エラー詳細", value=f"{type(e).__name__}: {e}")

            await interaction.edit_original_response(embeds=[claim_embed, refund_embed])
            return

        log.info("Deposited %s %s into the reward pool of guild %s (claim %s)", amount, initial_unit, interaction.guild_id, new_claim.id)
        confirm_embed = Embed(title="処理が完了しました", colour=embedColour.Success)
        confirm_embed.description = f"請求`{new_claim.id}`は承認され、プールに **{amount} {initial_unit}** が補充されました。"
        await interaction.edit_original_response(embeds=[claim_embed, confirm_embed])

    except Exception as e:
        log.exception("reward_pool deposit failed")
        await interaction.edit_original_response(embed=Embed(title="内部エラー", description=f"{e.__class__.__name__}:\n{e}", colour=embedColour.Error))


@reward_pool_init.error
@reward_pool_set.error
@reward_pool_delete.error
@reward_pool_deposit.error
async def rp_cmd_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("このコマンドは管理者のみ実行できます。", ephemeral=True)
    else:
        await interaction.response.send_message(f"エラーが発生しました: {error}", ephemeral=True)

# --- Generic Reward Handler ---

async def handle_reward(reward_type: RewardType, user: discord.Member, guild: discord.Guild):
    """Pay a configured reward: the pool deduction and cooldown are committed first, the payment follows, and a failed payment is compensated."""
    if user.bot:
        return

    # The reservation runs synchronously in one BEGIN IMMEDIATE transaction, so concurrent events cannot both pass the checks.
    try:
        with Database() as db:
            reservation = db.reserve_reward(guild.id, user.id, reward_type, int(time()))
    except Exception:
        log.exception("Reserving reward '%s' for user %s in guild %s failed", reward_type, user.id, guild.id)
        return
    if reservation is None:
        return

    try:
        await get_vc_client().pay(reservation.unit, user.id, reservation.amount)
    except Exception as e:
        log.error("Reward payment of %s %s to user %s in guild %s for '%s' failed (%s: %s); restoring the pool and cooldown", reservation.amount, reservation.unit, user.id, guild.id, reward_type, type(e).__name__, e)
        try:
            with Database() as db:
                pool_restored, cooldown_restored = db.release_reward(reservation)
        except Exception:
            log.exception("Compensation failed: reward pool of guild %s is short by %s %s that were not paid", guild.id, reservation.amount, reservation.unit)
            return
        if not pool_restored:
            log.error("Compensation incomplete: reward pool of guild %s no longer uses %s, so %s %s were not returned to it", guild.id, reservation.unit, reservation.amount, reservation.unit)
        if not cooldown_restored:
            log.info("Cooldown of user %s in guild %s for '%s' was updated by a later reward and is kept", user.id, guild.id, reward_type)
        return

    log.info("Rewarded %s %s to user %s in guild %s for '%s'", reservation.amount, reservation.unit, user.id, guild.id, reward_type)

# --- Admin Commands ---

def is_admin(interaction: discord.Interaction) -> bool:
    return is_admin_id(interaction.user.id)

@app_commands.command(name="refresh", description=f"[デバッグ用] 空コマンド [{int(time())}]")
@app_commands.check(is_admin)
async def admin_refresh(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    await interaction.followup.send(content=str(int(time())))

@admin_refresh.error
async def admin_cmd_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("このコマンドは管理者のみ実行できます。", ephemeral=True)
    else:
        await interaction.response.send_message(f"エラーが発生しました: {error}", ephemeral=True)
