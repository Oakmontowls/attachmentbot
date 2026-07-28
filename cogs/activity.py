import asyncio

import discord
from discord.ext import commands

from core.message_activity import message_activity


class ActivityCog(commands.Cog):
	def __init__(self, bot):
		self.bot = bot
		self.worker = None

	async def cog_load(self):
		self.worker = asyncio.create_task(message_activity.initialize(self.bot))

	async def cog_unload(self):
		if self.worker:
			self.worker.cancel()

	@commands.Cog.listener()
	async def on_message(self, message: discord.Message):
		message_activity.record_live(message)


async def setup(bot):
	await bot.add_cog(ActivityCog(bot))
