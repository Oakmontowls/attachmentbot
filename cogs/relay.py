import discord
from discord import app_commands
from discord.ext import commands

from core.database import db
from core.feature_cog import attach_feature_cog, detach_feature_cog
from core.permissions import can_manage_bot, require_manager
from core.relay import relay_service


async def source_manager(interaction: discord.Interaction, channel: discord.TextChannel) -> bool:
	if channel.guild.id == interaction.guild_id:
		return True
	member = channel.guild.get_member(interaction.user.id)
	if member is None:
		try:
			member = await channel.guild.fetch_member(interaction.user.id)
		except discord.DiscordException:
			member = None
	if isinstance(member, discord.Member) and can_manage_bot(member):
		return True
	await interaction.response.send_message(
		"You must also be an Administrator or approved AttachmentBot manager "
		"in the source channel's server.",
		ephemeral=True,
	)
	return False


class RelayCog(
	commands.GroupCog,
	group_name="relay",
	group_description="Relay news channels into forum post threads.",
):
	def __init__(self, bot):
		self.bot = bot

	async def cog_load(self):
		relay_service.start(self.bot)

	async def cog_unload(self):
		detach_feature_cog(self)
		relay_service.stop()

	@commands.Cog.listener()
	async def on_message(self, message: discord.Message):
		await relay_service.handle_message(message)

	@app_commands.command(name="enabled", description="Enable or disable configured news relays.")
	@app_commands.describe(enabled="Whether this server's relay routes should deliver messages.")
	async def relay_enabled(self, interaction: discord.Interaction, enabled: bool):
		if not await require_manager(interaction):
			return
		try:
			await relay_service.set_enabled(interaction.guild_id, enabled)
		except ValueError as exc:
			await interaction.response.send_message(str(exc), ephemeral=True)
			return
		await interaction.response.send_message(
			f"News relay is now {'enabled' if enabled else 'disabled'}.",
			ephemeral=True,
		)

	@app_commands.command(name="add", description="Relay a text channel into a forum post thread.")
	@app_commands.describe(
		source_channel_id="ID of a text or announcement channel visible to the bot.",
		target_thread="Existing forum post thread that should receive relayed messages.",
	)
	async def relay_add(
		self,
		interaction: discord.Interaction,
		source_channel_id: str,
		target_thread: discord.Thread,
	):
		if not await require_manager(interaction):
			return
		if not source_channel_id.isdecimal():
			await interaction.response.send_message(
				"Source channel ID must contain only numbers.", ephemeral=True,
			)
			return
		source = await relay_service.resolve_channel(int(source_channel_id))
		if not isinstance(source, discord.TextChannel):
			await interaction.response.send_message(
				"The bot cannot access that source text channel.", ephemeral=True,
			)
			return
		if target_thread.guild.id != interaction.guild_id:
			await interaction.response.send_message(
				"The target thread must be in this server.", ephemeral=True,
			)
			return
		if not isinstance(target_thread.parent, discord.ForumChannel):
			await interaction.response.send_message(
				"The target must be an existing post inside a forum channel.", ephemeral=True,
			)
			return
		if not await source_manager(interaction, source):
			return
		try:
			route_id = await relay_service.add_route(
				interaction.guild_id,
				source,
				target_thread,
				interaction.user.id,
			)
		except ValueError as exc:
			await interaction.response.send_message(str(exc), ephemeral=True)
			return
		await interaction.response.send_message(
			f"Created relay route `{route_id}` from `{source.guild.name}` {source.mention} "
			f"to {target_thread.mention}.",
			ephemeral=True,
		)

	@app_commands.command(name="remove", description="Remove a configured relay route.")
	@app_commands.describe(route_id="Route ID shown by /ab relay list.")
	async def relay_remove(
		self,
		interaction: discord.Interaction,
		route_id: app_commands.Range[int, 1],
	):
		if not await require_manager(interaction):
			return
		removed = relay_service.remove_route(interaction.guild_id, route_id)
		await interaction.response.send_message(
			f"Relay route `{route_id}` was {'removed' if removed else 'not found'}.",
			ephemeral=True,
		)

	@app_commands.command(name="list", description="List this server's relay routes.")
	async def relay_list(self, interaction: discord.Interaction):
		if not await require_manager(interaction):
			return
		settings = db.get_relay_settings(interaction.guild_id)
		routes = settings["routes"]
		if not routes:
			await interaction.response.send_message(
				"No relay routes are configured.", ephemeral=True,
			)
			return
		lines = [
			f"`{route['route_id']}` <#{route['source_channel_id']}> "
			f"(`{route['source_guild_id']}`) -> <#{route['target_thread_id']}>"
			for route in routes
		]
		description = "\n".join(lines)
		if len(description) > 4000:
			description = description[:3970] + "\n...additional routes omitted"
		embed = discord.Embed(
			title=f"News Relay Routes ({'Enabled' if settings['enabled'] else 'Disabled'})",
			description=description,
			color=discord.Color.blurple(),
		)
		await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
	await attach_feature_cog(bot, RelayCog(bot))
