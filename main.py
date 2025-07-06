import discord
from discord import app_commands
import bot_commands as cmds
import config

intents = discord.Intents.default()
intents.members = True
intents.presences = True
intents.message_content = True 

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    print(f'" {client.user} "としてログイン中')
    await client.change_presence(activity=discord.Game(name="Some VC Utils by h4ribote"),status=discord.Status.online)

    tree.add_command(cmds.rain)
    tree.add_command(cmds.send_with_msg)
    tree.add_command(cmds.receive_msg)
    tree.add_command(cmds.admin_refresh)

    tree.add_command(cmds.reward_pool)

    @tree.command(name="info",description="このボットに関する情報を表示します")
    async def info_command(interaction:discord.Interaction):
        await interaction.response.defer(thinking=True)
        await interaction.followup.send(embed=cmds.bot_info())

    try:
        await tree.sync()
        print("スラッシュコマンドを同期しました。")
    except Exception as e:
        print(f"コマンド同期エラー: {e}")

@client.event
async def on_message(message:discord.Message):
    if message.author.bot:
        return

    if message.guild:
        await cmds.handle_reward('message', message.author, message.guild)

    if message.content.startswith(f"<@{client.user.id}>") and message.author.id in config.Discord.ADMIN:
        parts = message.content.split(' ')
        if len(parts) > 1 and parts[1] == "kill":
            await client.close()
            exit()

client.run(config.Discord.BOT_TOKEN)
