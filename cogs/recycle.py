import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from core.feature_cog import attach_feature_cog, detach_feature_cog
from core.permissions import require_manager
from core.recycle import recycle_service


class RecycleCog(
	commands.GroupCog,
	group_name="recycle",
	group_description="Manage duplicate image and link detection.",
):
	def __init__(self, bot):
		self.bot = bot
		self.start_task = None

	async def cog_load(self):
		self.start_task = asyncio.create_task(recycle_service.start(self.bot))

	async def cog_unload(self):
		detach_feature_cog(self)
		if self.start_task:
			self.start_task.cancel()
		await recycle_service.stop()

	@commands.Cog.listener()
	async def on_message(self, message: discord.Message):
		await recycle_service.handle_message(message)

	@app_commands.command(name="enabled", description="Enable or disable recycle duplicate detection.")
	@app_commands.describe(enabled="Whether recycle duplicate detection should run.")
	async def recycle_enabled(self, interaction: discord.Interaction, enabled: bool):
		if not await require_manager(interaction):
			return
		try:
			await recycle_service.set_enabled(interaction.guild, enabled)
		except ValueError as exc:
			await interaction.response.send_message(str(exc), ephemeral=True)
			return
		await interaction.response.send_message(
			f"Recycle duplicate detection is now {'enabled' if enabled else 'disabled'}.",
			ephemeral=True,
		)

	@app_commands.command(
		name="reply-fallback",
		description="Reply with a ping when the bot cannot add its recycle reaction.",
	)
	@app_commands.describe(enabled="Whether failed reactions should fall back to a pinging reply.")
	async def recycle_reply_fallback(self, interaction: discord.Interaction, enabled: bool):
		if not await require_manager(interaction):
			return
		recycle_service.set_reply_fallback(interaction.guild_id, enabled)
		await interaction.response.send_message(
			f"Recycle reply fallback is now {'enabled' if enabled else 'disabled'}.",
			ephemeral=True,
		)

	@app_commands.command(
		name="ignore-replies",
		description="Choose whether reply messages can be flagged as reposts.",
	)
	@app_commands.describe(enabled="Index replies for future matches without flagging the replies themselves.")
	async def recycle_ignore_replies(self, interaction: discord.Interaction, enabled: bool):
		if not await require_manager(interaction):
			return
		recycle_service.set_ignore_replies(interaction.guild_id, enabled)
		await interaction.response.send_message(
			f"Recycle will now {'ignore' if enabled else 'include'} reply messages. "
			"Replies will still be indexed for matching future posts.",
			ephemeral=True,
		)

	@app_commands.command(name="history-days", description="Set the rolling recycle history window.")
	@app_commands.describe(days="Number of days in which an earlier matching post counts as a repost.")
	async def recycle_history_days(
		self,
		interaction: discord.Interaction,
		days: app_commands.Range[int, 1, 365],
	):
		if not await require_manager(interaction):
			return
		changed = await recycle_service.set_history_days(interaction.guild, days)
		message = f"Recycle repost history is now {days} day{'s' if days != 1 else ''}."
		if changed:
			message += " The recycle index was reset for the new window."
		await interaction.response.send_message(message, ephemeral=True)

	@app_commands.command(name="channel", description="Set the channel checked for duplicate posts.")
	@app_commands.describe(channel="The text channel to index and monitor.")
	async def recycle_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
		if not await require_manager(interaction):
			return
		await recycle_service.set_channel(interaction.guild, channel)
		await interaction.response.send_message(
			f"Recycle duplicate detection will monitor {channel.mention}.",
			ephemeral=True,
		)


async def setup(bot):
	await attach_feature_cog(bot, RecycleCog(bot))
