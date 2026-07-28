import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from core.feature_cog import attach_feature_cog, detach_feature_cog
from core.permissions import require_manager
from core.territory import territory_service


class TerritoryCog(
	commands.GroupCog,
	group_name="territory",
	group_description="Configure territory channel tracking.",
):
	def __init__(self, bot):
		self.bot = bot
		self.worker = None

	async def cog_load(self):
		self.worker = asyncio.create_task(territory_service.start(self.bot))

	async def cog_unload(self):
		detach_feature_cog(self)
		if self.worker:
			self.worker.cancel()

	@app_commands.command(name="interval", description="Set the territory description update interval.")
	@app_commands.describe(minutes="Minutes between territory updates. Defaults to 60.")
	async def territory_interval(
		self,
		interaction: discord.Interaction,
		minutes: app_commands.Range[int, 1, 1440],
	):
		if not await require_manager(interaction):
			return
		territory_service.set_update_interval(interaction.guild_id, minutes)
		await interaction.response.send_message(
			f"Territory update interval set to {minutes} minute{'s' if minutes != 1 else ''}.",
			ephemeral=True,
		)

	@app_commands.command(name="update", description="Update territory channel descriptions now.")
	async def territory_update(self, interaction: discord.Interaction):
		if not await require_manager(interaction):
			return
		await interaction.response.defer(ephemeral=True)
		result = await territory_service.force_update(interaction.guild)
		if result == "disabled":
			message = "Territory tracking is disabled for this server."
		elif result == "loading":
			message = "The 24-hour message history cache is still loading."
		else:
			message = "Territory channel descriptions have been updated."
		await interaction.followup.send(message, ephemeral=True)

	@app_commands.command(name="enabled", description="Enable or disable territory channel descriptions.")
	@app_commands.describe(enabled="Whether territory descriptions should be updated.")
	async def territory_enabled(self, interaction: discord.Interaction, enabled: bool):
		if not await require_manager(interaction):
			return
		await interaction.response.defer(ephemeral=True)
		await territory_service.set_enabled(interaction.guild, enabled)
		await interaction.followup.send(
			f"Territory descriptions are now {'enabled' if enabled else 'disabled'}.",
			ephemeral=True,
		)

	@app_commands.command(name="blacklist", description="Add or remove a territory channel blacklist entry.")
	@app_commands.describe(action="Whether to add or remove the channel.", channel="The text channel to update.")
	@app_commands.choices(action=[
		app_commands.Choice(name="add", value="add"),
		app_commands.Choice(name="remove", value="remove"),
	])
	async def territory_blacklist(
		self,
		interaction: discord.Interaction,
		action: str,
		channel: discord.TextChannel,
	):
		if not await require_manager(interaction):
			return
		await interaction.response.defer(ephemeral=True)
		blacklisted = action == "add"
		await territory_service.set_channel_blacklisted(interaction.guild, channel, blacklisted)
		await interaction.followup.send(
			f"{channel.mention} was {'added to' if blacklisted else 'removed from'} "
			"the territory blacklist.",
			ephemeral=True,
		)


@app_commands.guild_only()
class TerritoryLeaderboardCog(
	commands.GroupCog,
	group_name="territory",
	group_description="View channel message territory standings.",
):
	def __init__(self, bot):
		self.bot = bot

	@app_commands.command(name="leaderboard", description="Show this channel's 24-hour message leaderboard.")
	@app_commands.describe(channel="Channel to show. Defaults to the current channel.")
	async def territory_leaderboard(
		self,
		interaction: discord.Interaction,
		channel: discord.TextChannel | None = None,
	):
		target = channel or interaction.channel
		if not target or not hasattr(target, "id"):
			await interaction.response.send_message("Choose a server text channel.", ephemeral=True)
			return
		leaders = territory_service.leaderboard(interaction.guild_id, target.id, limit=5)
		if leaders is None:
			await interaction.response.send_message(
				"The 24-hour message history cache is still loading.", ephemeral=True,
			)
			return
		if not leaders:
			await interaction.response.send_message(
				f"No user messages were recorded in {target.mention} during the last 24 hours.",
				ephemeral=True,
			)
			return
		lines = [
			f"{position}. <@{user_id}> - {count} message{'s' if count != 1 else ''}"
			for position, (user_id, count) in enumerate(leaders, start=1)
		]
		embed = discord.Embed(
			title="24-Hour Message Leaderboard",
			description=f"{target.mention}\n\n" + "\n".join(lines),
			color=discord.Color.blurple(),
		)
		await interaction.response.send_message(embed=embed)


async def setup(bot):
	await attach_feature_cog(bot, TerritoryCog(bot))
	await bot.add_cog(TerritoryLeaderboardCog(bot))
