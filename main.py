import discord
from discord.ext import commands
import asyncio
import os
from pathlib import Path
from core.logger import setup_logger
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

	async def setup_hook(self):
		await self.load_extension("cogs.moderation")
		synced = await self.tree.sync()
		log.info(f"Synced {len(synced)} slash command(s)")

		# start background worker
		from core.queue_worker import start_worker
		from core.temporary_roles import start_temporary_role_worker
		asyncio.create_task(start_worker(self))
		asyncio.create_task(start_temporary_role_worker(self))

bot = AttachmentBot()

@bot.event
async def on_ready():
	log.info(f"Logged in as {bot.user}")

bot.run(load_token())
