import discord
from discord.ext import commands
import asyncio
import os
from pathlib import Path
from core.logger import LOG_FILE, setup_logger
from config import TOKEN as CONFIG_TOKEN

log = setup_logger()

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
intents.members = True

def load_token():
	env_token = os.getenv("DISCORD_TOKEN")
	if env_token:
		return env_token

	if CONFIG_TOKEN and CONFIG_TOKEN != "redacted":
		return CONFIG_TOKEN

	token_file = Path("testbottoken.txt")
	if token_file.exists():
		return token_file.read_text(encoding="utf-8").strip()

	return CONFIG_TOKEN

class AttachmentBot(commands.Bot):
	def __init__(self):
		super().__init__(command_prefix="!", intents=intents)
		self.guild_command_sync_complete = False

	async def setup_hook(self):
		await self.load_extension("cogs.moderation")
		synced = await self.tree.sync()
		log.info(f"Synced {len(synced)} slash command(s)")

		# start background worker
		from core.queue_worker import start_worker
		from core.temporary_roles import start_temporary_role_worker
		from core.message_activity import message_activity
		asyncio.create_task(start_worker(self))
		asyncio.create_task(start_temporary_role_worker(self))
		asyncio.create_task(message_activity.initialize(self))

bot = AttachmentBot()

@bot.event
async def on_ready():
	log.info(f"Logged in as {bot.user}")
	log.info(f"Log file: {LOG_FILE}")
	log.info(f"Working directory: {Path.cwd()}")
	if bot.guild_command_sync_complete:
		return

	for guild in bot.guilds:
		try:
			bot.tree.clear_commands(guild=guild)
			synced = await bot.tree.sync(guild=guild)
			log.info(f"Cleared guild-specific slash commands for guild {guild.id}; {len(synced)} remain")
		except discord.DiscordException as exc:
			log.info(f"Guild command sync failed for {guild.id}: {exc}")

	bot.guild_command_sync_complete = True

bot.run(load_token())
