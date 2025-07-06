import config
import discord
from discord import app_commands, Embed, User, Role, Member, Interaction
from virtualcrypto import AsyncVirtualCryptoClient, Scope, ClaimStatus, Claim
import sqlite3
import embedColour
import asyncio
from time import time
from typing import Optional

from db_structs import RewardPool, RewardConfig, UserRewardCooldown


async def VCClient() -> AsyncVirtualCryptoClient:
    """
    非同期のVirtualCryptoクライアントを初期化して返します。
    """
    cli = AsyncVirtualCryptoClient(
        client_id=config.VirtualCrypto.client_id,
        client_secret=config.VirtualCrypto.client_secret,
        scopes=[Scope.Pay, Scope.Claim]
    )
    await cli.start()
    return cli

DBConnection = sqlite3.connect("database.db")
def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d

DBConnection.row_factory = dict_factory
cursor = DBConnection.cursor()

cursor.execute("""
    CREATE TABLE IF NOT EXISTS reward_pools (
        guild_id INTEGER PRIMARY KEY,
        unit TEXT NOT NULL,
        pool_balance INTEGER NOT NULL DEFAULT 0
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS reward_configs (
        config_id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        reward_type TEXT NOT NULL,
        amount INTEGER NOT NULL,
        cooldown_seconds INTEGER DEFAULT 0,
        FOREIGN KEY (guild_id) REFERENCES reward_pools(guild_id) ON DELETE CASCADE,
        UNIQUE (guild_id, reward_type)
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_reward_cooldowns (
        user_id INTEGER NOT NULL,
        guild_id INTEGER NOT NULL,
        reward_type TEXT NOT NULL,
        last_triggered_timestamp INTEGER NOT NULL,
        PRIMARY KEY (user_id, guild_id, reward_type)
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS receive_msg (
        user_id INTEGER PRIMARY KEY
    )
""")
DBConnection.commit()
cursor.close()

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
    claim_embed.add_field(name="承認用コマンド", value=f"/claim approve id:{new_claim.id}", inline=False)
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

# --- Core Commands ---

@app_commands.command(name="rain",description="通貨を特定のロールのメンバーに配ります")
@app_commands.describe(unit="通貨の単位", amount_per_user="1ユーザーあたりの数量", role="ロール")
async def rain(interaction:Interaction, unit:str, amount_per_user:int, role:Role):
    await interaction.response.defer(thinking=True)
    vc_client = None
    try:
        role_mems = role.members
        member_count = len(role_mems)
        if member_count == 0:
            await interaction.edit_original_response(embed=Embed(title="エラー", description="対象ロールにメンバーがいません。", colour=embedColour.Error))
            return
        
        vc_client = await VCClient()
        total_amount = amount_per_user * member_count
        claim_embed, new_claim = await create_claim_embed(vc_client, interaction.user.id, unit, total_amount, f"通貨のエアドロップ({amount_per_user} * {member_count})")
        await interaction.followup.send(embeds=[claim_embed])

        if not await wait_for_claim_approval(interaction, vc_client, new_claim, claim_embed):
            return

        for mem in role_mems:
            await vc_client.pay(unit, mem.id, amount_per_user)

        confirm_embed = Embed(title="処理が完了しました", colour=embedColour.Success)
        confirm_embed.description = f"請求`{new_claim.id}`は承認され、{member_count}人のメンバーに **{amount_per_user} {unit}** を配布しました。"
        await interaction.edit_original_response(embeds=[claim_embed, confirm_embed])

    except Exception as e:
        await interaction.edit_original_response(embed=Embed(title="内部エラー", description=f"{e.__class__.__name__}:\n{e}", colour=embedColour.Error))
    finally:
        if vc_client: await vc_client.close()

@app_commands.command(name="send_with_msg",description="DMでのメッセージと一緒に通貨を送信します")
@app_commands.describe(unit="通貨単位", user="対象ユーザー", amount="数量", message="メッセージ")
async def send_with_msg(interaction:Interaction, unit:str, user:Member, amount:int, message:str):
    await interaction.response.defer(thinking=True)
    vc_client = None
    cursor = DBConnection.cursor()
    try:
        cursor.execute("SELECT * FROM receive_msg WHERE user_id = ?", (user.id,))
        if not cursor.fetchone():
            await interaction.edit_original_response(embed=Embed(title="エラー", description=f"対象のユーザーは`/receive_msg`が無効に設定されています", colour=embedColour.Error))
            return
        
        vc_client = await VCClient()
        claim_embed, new_claim = await create_claim_embed(vc_client, interaction.user.id, unit, amount, f"`/send_with_msg`による送信")
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
        await interaction.edit_original_response(embed=Embed(title="内部エラー", description=f"{e.__class__.__name__}:\n{e}", colour=embedColour.Error))
    finally:
        cursor.close()
        if vc_client: await vc_client.close()

@app_commands.command(name="receive_msg",description="/send_with_msgの内容をDMで通知します")
@app_commands.describe(receive_config="[True]DMを受け取る [False]DMを受け取らない")
async def receive_msg(interaction:Interaction, receive_config:bool):
    await interaction.response.defer(thinking=True, ephemeral=True)
    cursor = DBConnection.cursor()
    try:
        if receive_config:
            cursor.execute("INSERT OR IGNORE INTO receive_msg (user_id) VALUES (?)", (interaction.user.id,))
            await interaction.followup.send(embed=Embed(title="設定完了", description="/send_with_msgの内容がDMで通知されます", colour=embedColour.LightBlue))
        else:
            cursor.execute("DELETE FROM receive_msg WHERE user_id = ?", (interaction.user.id,))
            await interaction.followup.send(embed=Embed(title="設定完了", description="/send_with_msgの内容はDMで通知されません", colour=embedColour.LightBlue))
        DBConnection.commit()
    except Exception as e:
        DBConnection.rollback()
        await interaction.edit_original_response(embed=Embed(title="内部エラー", description=f"{type(e).__name__}:\n{e}", colour=embedColour.Error))
    finally:
        cursor.close()

# --- Reward Pool Commands ---

reward_pool = app_commands.Group(name="reward_pool", description="サーバー内の報酬に関する設定")

@reward_pool.command(name="init", description="このサーバーの報酬プールの通貨単位を初期設定します (管理者向け)")
@app_commands.describe(unit="報酬として使用する通貨の単位")
@app_commands.checks.has_permissions(manage_guild=True)
async def reward_pool_init(interaction: Interaction, unit: str):
    await interaction.response.defer(thinking=True, ephemeral=True)
    cursor = DBConnection.cursor()
    vc_client = None
    try:
        cursor.execute("SELECT * FROM reward_pools WHERE guild_id = ?", (interaction.guild_id,))
        pool = RewardPool.from_dict(cursor.fetchone())

        if pool and pool.pool_balance > 0 and pool.unit != unit:
                
            vc_client = await VCClient()
            await vc_client.pay(pool.unit, interaction.user.id, pool.pool_balance)
                
            cursor.execute(
                "UPDATE reward_pools SET unit = ?, pool_balance = 0 WHERE guild_id = ?",
                (unit, interaction.guild_id)
            )
            DBConnection.commit()
            
            success_embed = Embed(
                title="処理完了",
                description=f"プール残高 **{pool.pool_balance} {pool.unit}** をあなたのウォレットに返金し、通貨単位を **{unit}** に更新しました。",
                color=embedColour.Success
            )
            await interaction.followup.send(embed=success_embed)
            return

        cursor.execute(
            "INSERT INTO reward_pools (guild_id, unit) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET unit=excluded.unit",
            (interaction.guild_id, unit)
        )
        DBConnection.commit()
        await interaction.followup.send(embed=Embed(title="設定完了", description=f"報酬プールの通貨単位を **{unit}** に設定しました。", colour=embedColour.Success))

    except Exception as e:
        DBConnection.rollback()
        await interaction.edit_original_response(content=None, embed=Embed(title="内部エラー", description=f"{type(e).__name__}:\n{e}", colour=embedColour.Error), view=None)
    finally:
        cursor.close()
        if vc_client: await vc_client.close()

@reward_pool.command(name="set", description="報酬ルールを追加または更新します (管理者向け)")
@app_commands.describe(reward_type="報酬の種類 (例: message, voice_joinなど)", amount="報酬量", cooldown_seconds="次の報酬までの待機時間(秒)")
@app_commands.checks.has_permissions(manage_guild=True)
async def reward_pool_set(interaction: Interaction, reward_type: str, amount: int, cooldown_seconds: int):
    await interaction.response.defer(thinking=True, ephemeral=True)
    if amount <= 0 or cooldown_seconds < 0:
        await interaction.followup.send(embed=Embed(title="エラー", description="報酬量とクールダウンは0以上の値を設定してください。", colour=embedColour.Error))
        return
    if len(reward_type) > 20 or cooldown_seconds > 31557600:
        await interaction.followup.send(embed=Embed(title="エラー", description="無効な値です", colour=embedColour.Error))
        return
    
    cursor = DBConnection.cursor()
    try:
        cursor.execute("SELECT 1 FROM reward_pools WHERE guild_id = ?", (interaction.guild_id,))
        if not cursor.fetchone():
            await interaction.followup.send(embed=Embed(title="エラー", description="先に`/reward_pool init`で通貨単位を設定してください。", colour=embedColour.Error))
            return

        cursor.execute(
            """
            INSERT INTO reward_configs (guild_id, reward_type, amount, cooldown_seconds) VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, reward_type) DO UPDATE SET amount=excluded.amount, cooldown_seconds=excluded.cooldown_seconds
            """,
            (interaction.guild_id, reward_type.lower(), amount, cooldown_seconds)
        )
        DBConnection.commit()
        embed = Embed(title="設定完了", description=f"報酬ルール **{reward_type.lower()}** を保存しました。", colour=embedColour.Success)
        embed.add_field(name="報酬量", value=str(amount))
        embed.add_field(name="クールダウン", value=f"{cooldown_seconds}秒")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        DBConnection.rollback()
        await interaction.followup.send(embed=Embed(title="内部エラー", description=f"{type(e).__name__}:\n{e}", colour=embedColour.Error))
    finally:
        cursor.close()

@reward_pool.command(name="delete", description="報酬ルールを削除します (管理者向け)")
@app_commands.describe(reward_type="削除する報酬の種類 (例: message)")
@app_commands.checks.has_permissions(manage_guild=True)
async def reward_pool_delete(interaction: Interaction, reward_type: str):
    await interaction.response.defer(thinking=True, ephemeral=True)
    cursor = DBConnection.cursor()
    try:
        cursor.execute(
            "DELETE FROM reward_configs WHERE guild_id = ? AND reward_type = ?",
            (interaction.guild_id, reward_type.lower())
        )
        if cursor.rowcount > 0:
            DBConnection.commit()
            await interaction.followup.send(embed=Embed(title="削除完了", description=f"報酬ルール **{reward_type.lower()}** を削除しました。", colour=embedColour.Success))
        else:
            await interaction.followup.send(embed=Embed(title="エラー", description=f"報酬ルール **{reward_type.lower()}** は見つかりませんでした。", colour=embedColour.Error))
    except Exception as e:
        DBConnection.rollback()
        await interaction.followup.send(embed=Embed(title="内部エラー", description=f"{type(e).__name__}:\n{e}", colour=embedColour.Error))
    finally:
        cursor.close()

@reward_pool.command(name="info", description="報酬プールの現在の情報を表示します")
async def reward_pool_info(interaction: Interaction):
    await interaction.response.defer(thinking=True)
    cursor = DBConnection.cursor()
    try:
        cursor.execute("SELECT * FROM reward_pools WHERE guild_id = ?", (interaction.guild_id,))
        pool_dict = cursor.fetchone()
        pool = RewardPool.from_dict(pool_dict)

        if not pool:
            await interaction.followup.send(embed=Embed(title="情報", description="このサーバーでは報酬プールがまだ設定されていません。", colour=embedColour.Yellow))
            return
        
        embed = Embed(title="報酬プール情報", description=f"{interaction.guild.name}の現在の設定です。", colour=embedColour.LightBlue)
        embed.add_field(name="プール残高", value=f"{pool.pool_balance} {pool.unit}", inline=False)
        
        cursor.execute("SELECT * FROM reward_configs WHERE guild_id = ?", (interaction.guild_id,))
        configs = [RewardConfig.from_dict(c) for c in cursor.fetchall()]

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
        await interaction.followup.send(embed=Embed(title="内部エラー", description=f"{type(e).__name__}:\n{e}", colour=embedColour.Error))
    finally:
        cursor.close()

@reward_pool.command(name="deposit", description="報酬プールに通貨を補充します (管理者向け)")
@app_commands.describe(amount="補充する数量")
@app_commands.checks.has_permissions(manage_guild=True)
async def reward_pool_deposit(interaction: Interaction, amount: int):
    await interaction.response.defer(thinking=True)
    vc_client = None
    cursor = DBConnection.cursor()
    try:
        cursor.execute("SELECT * FROM reward_pools WHERE guild_id = ?", (interaction.guild_id,))
        pool = RewardPool.from_dict(cursor.fetchone())

        if not pool:
            await interaction.edit_original_response(embed=Embed(title="エラー", description="先に`/reward_pool init`で報酬プールを設定してください。", colour=embedColour.Error))
            return
        if amount <= 0:
            await interaction.edit_original_response(embed=Embed(title="エラー", description="補充する数量は0より大きい値を設定してください。", colour=embedColour.Error))
            return

        initial_unit = pool.unit
        
        vc_client = await VCClient()
        claim_embed, new_claim = await create_claim_embed(vc_client, interaction.user.id, initial_unit, amount, f"報酬プールへの補充")
        await interaction.followup.send(embeds=[claim_embed])

        if not await wait_for_claim_approval(interaction, vc_client, new_claim, claim_embed):
            return
        
        cursor.execute("SELECT unit FROM reward_pools WHERE guild_id = ?", (interaction.guild_id,))
        current_pool_data = cursor.fetchone()
        current_unit = current_pool_data['unit'] if current_pool_data else None

        if current_unit != initial_unit:
            refund_embed = Embed(
                title="処理中断と返金",
                description=f"入金処理中に報酬プールの通貨単位が **{initial_unit}** から **{current_unit}** に変更されました。\n"
                            f"承認された **{amount} {initial_unit}** はプールに入金せず、あなたのウォレットに返金します。",
                color=embedColour.Orange
            )
            try:
                await vc_client.pay(initial_unit, interaction.user.id, amount)
                refund_embed.color = embedColour.Success
                refund_embed.title = "処理中断と返金完了"
            except Exception as e:
                refund_embed.color = embedColour.Error
                refund_embed.title = "返金エラー"
                refund_embed.add_field(name="エラー詳細", value=f"{type(e).__name__}: {e}")

            await interaction.edit_original_response(embeds=[claim_embed, refund_embed])
            return

        cursor.execute(
            "UPDATE reward_pools SET pool_balance = pool_balance + ? WHERE guild_id = ?",
            (amount, interaction.guild_id)
        )
        DBConnection.commit()
        
        confirm_embed = Embed(title="処理が完了しました", colour=embedColour.Success)
        confirm_embed.description = f"請求`{new_claim.id}`は承認され、プールに **{amount} {initial_unit}** が補充されました。"
        await interaction.edit_original_response(embeds=[claim_embed, confirm_embed])

    except Exception as e:
        DBConnection.rollback()
        await interaction.edit_original_response(embed=Embed(title="内部エラー", description=f"{e.__class__.__name__}:\n{e}", colour=embedColour.Error))
    finally:
        cursor.close()
        if vc_client: await vc_client.close()


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

async def handle_reward(reward_type: str, user: discord.Member, guild: discord.Guild):
    if user.bot:
        return

    cursor = DBConnection.cursor()
    vc_client = None
    try:
        cursor.execute("SELECT * FROM reward_configs WHERE guild_id = ? AND reward_type = ?", (guild.id, reward_type))
        config = RewardConfig.from_dict(cursor.fetchone())
        if not config: return

        cursor.execute("SELECT * FROM reward_pools WHERE guild_id = ?", (guild.id,))
        pool = RewardPool.from_dict(cursor.fetchone())
        if not pool or pool.pool_balance < config.amount:
            return

        current_time = int(time())
        cursor.execute(
            "SELECT * FROM user_reward_cooldowns WHERE user_id = ? AND guild_id = ? AND reward_type = ?",
            (user.id, guild.id, reward_type)
        )
        cooldown_data = UserRewardCooldown.from_dict(cursor.fetchone())

        if cooldown_data and (current_time - cooldown_data.last_triggered_timestamp) < config.cooldown_seconds:
            return
        
        vc_client = await VCClient()
        await vc_client.pay(pool.unit, user.id, config.amount)

        cursor.execute(
            "UPDATE reward_pools SET pool_balance = pool_balance - ? WHERE guild_id = ?",
            (config.amount, guild.id)
        )
        cursor.execute(
            """
            INSERT INTO user_reward_cooldowns (user_id, guild_id, reward_type, last_triggered_timestamp) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, guild_id, reward_type) DO UPDATE SET last_triggered_timestamp=excluded.last_triggered_timestamp
            """,
            (user.id, guild.id, reward_type, current_time)
        )
        DBConnection.commit()
        # print(f"[Guild_{guild.id}] Rewarded {config.amount} {pool.unit} to {user.name} for '{reward_type}'.")

    except Exception as e:
        DBConnection.rollback()
        print(f"Error in handle_reward for '{reward_type}' in '{guild.name}': {e}")
    finally:
        cursor.close()
        if vc_client: await vc_client.close()

# --- Admin Commands ---

def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.id in config.Discord.ADMIN

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
