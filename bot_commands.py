import config
import discord
from discord import app_commands, Embed, Color, User, Role, Member, Interaction, ui
from virtualcrypto import VirtualCryptoClient, AsyncVirtualCryptoClient, Scope, ClaimStatus, Claim
import sqlite3
import embedColour
import asyncio
from time import time

async def VCClient():
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

def bot_info() -> Embed:
    ...

async def create_claim_embed(vc_client:VirtualCryptoClient, payer_id:int, unit:str, amount:int, claim_info:str, expire_min:int = 2) -> tuple[Embed, Claim]:
    new_claim = await vc_client.create_claim(payer_id, unit, amount)
    claim_embed = Embed(title="請求を発行しました", colour=embedColour.LightBlue)
    claim_embed.description = "下記の通り請求を発行しました\nVirtualCryptoから承認してください"
    claim_embed.add_field(name="請求id", value=f"{new_claim.id}")
    claim_embed.add_field(name="数量", value=f"{amount} {unit}")
    claim_embed.add_field(name="内容", value=claim_info, inline=False)
    claim_embed.add_field(name="承認用コマンド", value=f"/claim approve id:{new_claim.id}", inline=False)
    claim_embed.set_footer(text=f"注意: 請求は最大{expire_min}分間有効です")
    return claim_embed, new_claim

@app_commands.command(name="rain",description="通貨を特定のロールのメンバーに配ります")
@app_commands.describe(unit="通貨の単位", amount_per_user="1ユーザーあたりの数量", role="ロール")
async def rain(interaction:Interaction, unit:str, amount_per_user:int, role:Role):
    await interaction.response.defer(thinking=True)
    try:
        VC_Client = await VCClient()
        role_mems = role.members
        member_count = len(role_mems)
        amount = amount_per_user*member_count
        claim_embed, new_claim = await create_claim_embed(VC_Client, interaction.user.id, unit, amount, f"通貨のエアドロップ({amount_per_user} * {member_count})")
        await interaction.followup.send(embeds=[claim_embed])

        for i in range(12):
            await asyncio.sleep(10)
            new_claim = await VC_Client.get_claim(new_claim.id)
            print(f"{new_claim=}")
            if new_claim.status == ClaimStatus.Approved:
                break
            elif new_claim.status in [ClaimStatus.Denied, ClaimStatus.Canceled]:
                cancel_embed = Embed(description="請求はキャンセルされました", colour=embedColour.Error)
                i = 12
            if i == 11:
                await VC_Client.update_claim(new_claim.id, ClaimStatus.Canceled)
                cancel_embed = Embed(description="操作はタイムアウトしました", colour=embedColour.Error)
                i = 12
            if i == 12:
                await interaction.edit_original_response(embeds=[claim_embed, cancel_embed])
                await VC_Client.close()
                return
        
        for role_mem in role_mems:
            await VC_Client.pay(unit, role_mem.id, amount_per_user)

        confirm_embed = Embed(title="処理が完了しました", colour=embedColour.Success)
        confirm_embed.description = f"請求`{new_claim.id}`は承認され、正常に処理されました"
        await interaction.edit_original_response(embeds=[claim_embed, confirm_embed])

    except Exception as e:
        await interaction.edit_original_response(embed=Embed(title="内部エラー", description=f"{e.__class__.__name__}:\n{e}", colour=embedColour.Error))

    finally:
        try:
            await VC_Client.close()
        except UnboundLocalError: pass
        
@app_commands.command(name="send_with_msg",description="DMでのメッセージと一緒に通貨を送信します(DMの受信を /receive_msg で有効にしている必要があります)")
@app_commands.describe(unit="通貨単位", user="対象ユーザー", amount="数量", message="メッセージ")
async def send_with_msg(interaction:Interaction, unit:str, user:Member, amount:int, message:str):
    await interaction.response.defer(thinking=True)
    try:
        cursor = DBConnection.cursor()
        cursor.execute("SELECT * FROM receive_msg WHERE user_id = ?", (user.id,))
        if not cursor.fetchone():
            await interaction.edit_original_response(embed=Embed(title="エラー", description=f"対象のユーザーは`receive_msg`が無効に設定されています", colour=embedColour.Error))
            cursor.close()
            return
        VC_Client = await VCClient()
        claim_embed, new_claim = await create_claim_embed(VC_Client, interaction.user.id, unit, amount, f"`/send_with_msg`")
        await interaction.followup.send(embeds=[claim_embed])

        for i in range(12):
            await asyncio.sleep(10)
            new_claim = await VC_Client.get_claim(new_claim.id)
            if new_claim.status == ClaimStatus.Approved:
                break
            elif new_claim.status in [ClaimStatus.Denied, ClaimStatus.Canceled]:
                cancel_embed = Embed(description="請求はキャンセルされました", colour=embedColour.Error)
                i = 12
            if i == 11:
                await VC_Client.update_claim(new_claim.id, ClaimStatus.Canceled)
                cancel_embed = Embed(description="操作はタイムアウトしました", colour=embedColour.Error)
                i = 12
            if i == 12:
                await interaction.edit_original_response(embeds=[claim_embed, cancel_embed])
                await VC_Client.close()
                cursor.close()
                VC_Client.close()
                return
        
        await VC_Client.pay(unit, user.id, amount)
        dm_c = await user.create_dm()
        dm_em = Embed(title="VirtualCryptoで通貨を受け取りました", colour=embedColour.Green)
        dm_em.add_field(name="送信者", value=interaction.user.mention, inline=False)
        dm_em.add_field(name="数量", value=f"{amount} {unit}", inline=False)
        dm_em.add_field(name="メッセージ", value=message, inline=False)
        await dm_c.send(embed=dm_em)

        confirm_embed = Embed(title="処理が完了しました", colour=embedColour.Success)
        confirm_embed.description = f"請求`{new_claim.id}`は承認され、正常に処理されました"
        await interaction.edit_original_response(embeds=[claim_embed, confirm_embed])
        
    except Exception as e:
        await interaction.edit_original_response(embed=Embed(title="内部エラー", description=f"{e.__class__.__name__}:\n{e}", colour=embedColour.Error))
    finally:
        try:
            cursor.close()
            if not VC_Client.session.connector is None: await VC_Client.close()
        except UnboundLocalError: pass
    

@app_commands.command(name="receive_msg",description="/send_with_msgの内容をDMで通知します")
@app_commands.describe(receive_config="[True]DMを受け取る [False]DMを受け取らない")
async def receive_msg(interaction:Interaction, receive_config:bool):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        cursor = DBConnection.cursor()
        if receive_config:
            cursor.execute("SELECT * FROM receive_msg WHERE user_id = ?", (interaction.user.id,))
            if not cursor.fetchone():
                cursor.execute("INSERT INTO receive_msg (user_id) VALUES (?)", (interaction.user.id,))
            await interaction.followup.send(embed=Embed(title="設定完了", description="/send_with_msgの内容がDMで通知されます", colour=embedColour.LightBlue))
        else:
            cursor.execute("DELETE FROM receive_msg WHERE user_id = ?", (interaction.user.id,))
            await interaction.followup.send(embed=Embed(title="設定完了", description="/send_with_msgの内容はDMで通知されません", colour=embedColour.LightBlue))
        DBConnection.commit()
    except Exception as e:
        DBConnection.rollback()
        await interaction.edit_original_response(embed=Embed(title="内部エラー", description=f"{type(e).__name__}:\n{e}", colour=embedColour.Error))
    finally:
        try: cursor.close()
        except UnboundLocalError: pass

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
