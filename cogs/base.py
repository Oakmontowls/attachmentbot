import discord
from discord import app_commands
from discord.ext import commands

from core.database import (
	db,
	FEATURE_OCR_DELETE_MESSAGE,
	FEATURE_OCR_GIVE_ROLE,
	MODERATION_PERMISSION_NAMELESS,
	MODERATION_PERMISSION_OCR_REVIEW,
)
from core.permissions import require_admin, require_manager


@app_commands.guild_only()
class AttachmentBotBaseCog(
	commands.GroupCog,
	group_name="ab",
	group_description="Configure AttachmentBot for this server.",
):
	def __init__(self, bot):
		self.bot = bot

	@app_commands.command(name="status", description="Show this server's AttachmentBot setup.")
	async def status(self, interaction: discord.Interaction):
		if not await require_manager(interaction):
			return
		config = db.get_guild_settings(interaction.guild_id)
		if not config:
			await interaction.response.send_message("This server has not been configured yet.", ephemeral=True)
			return

		embed = discord.Embed(title="AttachmentBot Status", color=discord.Color.blurple())
		timeout_role = f"<@&{config['role_id']}>" if config["role_id"] else "Not set"
		moderator_roles_by_level = {}
		for role_id, permission_level in config["moderator_roles"].items():
			moderator_roles_by_level.setdefault(permission_level, []).append(role_id)
		moderator_roles = "\n".join(
			f"Level {permission_level}: "
			+ ", ".join(f"<@&{role_id}>" for role_id in sorted(role_ids))
			for permission_level, role_ids in sorted(moderator_roles_by_level.items())
		) or "None"
		permission_levels = config["moderation_permission_levels"]
		timeout_log_channel = (
			f"<#{config['timeout_log_channel_id']}>"
			if config["timeout_log_channel_id"] else "Not set"
		)
		managers = ", ".join(f"<@&{role_id}>" for role_id in sorted(config["manager_roles"])) or "None"
		embed.add_field(name="Timeout Role", value=timeout_role, inline=False)
		embed.add_field(name="Moderator Roles", value=moderator_roles[:1024], inline=False)
		embed.add_field(
			name="Moderation Permission Requirements",
			value=(
				f"OCR review: Level {permission_levels[MODERATION_PERMISSION_OCR_REVIEW]}\n"
				f"/nameless: Level {permission_levels[MODERATION_PERMISSION_NAMELESS]}"
			),
			inline=False,
		)
		embed.add_field(name="Timeout Log Channel", value=timeout_log_channel, inline=False)
		embed.add_field(name="Manager Roles", value=managers[:1024], inline=False)

		loaded = []
		if self.bot.get_cog("OcrCog"):
			loaded.append("OCR")
			features = config["features"]
			embed.add_field(name="OCR Enabled", value=str(config["enabled"]), inline=True)
			embed.add_field(name="OCR Threshold", value=str(config["detection_threshold"]), inline=True)
			embed.add_field(
				name="OCR Log Channel",
				value=f"<#{config['log_channel_id']}>" if config["log_channel_id"] else "Not set",
				inline=False,
			)
			embed.add_field(name="OCR Give Role", value=str(features.get(FEATURE_OCR_GIVE_ROLE, False)), inline=True)
			embed.add_field(name="OCR Delete Message", value=str(features.get(FEATURE_OCR_DELETE_MESSAGE, False)), inline=True)
			ocr_moderation = config["ocr_moderation"]
			review_channel_id = (
				ocr_moderation["review_channel_id"] or config["log_channel_id"]
			)
			review_channel = (
				f"<#{review_channel_id}>" if review_channel_id else "Not set"
			)
			embed.add_field(
				name="OCR Moderation Review",
				value=(
					f"Enabled: {ocr_moderation['review_enabled']}\n"
					f"Channel: {review_channel}"
				),
				inline=False,
			)
			embed.add_field(
				name="OCR Automatic Action",
				value=ocr_moderation["automatic_action"].replace("_", " ").title(),
				inline=True,
			)
			embed.add_field(
				name="OCR Direct Message",
				value="Configured" if ocr_moderation["direct_message"] else "Not set",
				inline=True,
			)

		if self.bot.get_cog("PressureCog"):
			loaded.append("Pressure")
			pressure = config["pressure"]
			embed.add_field(name="Pressure Enabled", value=str(pressure["enabled"]), inline=True)
			embed.add_field(name="Pressure Threshold", value=str(pressure["threshold"]), inline=True)

		if self.bot.get_cog("TerritoryCog"):
			loaded.append("Territory")
			territory = config["territory"]
			embed.add_field(name="Territory Enabled", value=str(territory["enabled"]), inline=True)
			embed.add_field(
				name="Territory Interval",
				value=f"{territory['update_interval_minutes']} minutes",
				inline=True,
			)

		if self.bot.get_cog("RecycleCog"):
			loaded.append("Recycle")
			recycle = config["recycle"]
			recycle_state = (
				f"Enabled in <#{recycle['channel_id']}>"
				if recycle["enabled"] and recycle["channel_id"] else "Disabled"
			)
			embed.add_field(
				name="Recycle Detection",
				value=(
					f"{recycle_state}\nHistory: {recycle['history_days']} days\n"
					f"Ignore replies: {recycle['ignore_replies']}"
				),
				inline=False,
			)

		if self.bot.get_cog("RelayCog"):
			loaded.append("Relay")
			relay = config["relay"]
			embed.add_field(
				name="News Relay",
				value=(
					f"Enabled: {relay['enabled']}\n"
					f"Configured routes: {len(relay['routes'])}"
				),
				inline=False,
			)

		if self.bot.get_cog("RespondCog"):
			loaded.append("Respond")
			respond = config["respond"]
			embed.add_field(
				name="Automatic Responses",
				value=(
					f"Enabled: {respond['enabled']}\n"
					f"Configured responses: {len(respond['responses'])}"
				),
				inline=False,
			)

		embed.add_field(name="Loaded Modules", value=", ".join(loaded) or "None", inline=False)
		await interaction.response.send_message(embed=embed, ephemeral=True)

	@app_commands.command(name="timeout-role", description="Set the timeout role used by moderation modules.")
	@app_commands.describe(role="The timeout role to give to moderated users.")
	async def set_timeout_role(self, interaction: discord.Interaction, role: discord.Role):
		if not await require_manager(interaction):
			return
		db.update_guild_settings(interaction.guild_id, role_id=role.id)
		await interaction.response.send_message(f"Timeout role set to {role.mention}.", ephemeral=True)

	@app_commands.command(name="moderator-role", description="Add or remove a leveled moderator role.")
	@app_commands.describe(
		action="Whether to add/update or remove the moderator role.",
		role="Moderator role to update.",
		permission_level="Permission level for add; level 1 is highest.",
	)
	@app_commands.choices(action=[
		app_commands.Choice(name="add", value="add"),
		app_commands.Choice(name="remove", value="remove"),
	])
	async def set_moderator_role(
		self,
		interaction: discord.Interaction,
		action: str,
		role: discord.Role,
		permission_level: app_commands.Range[int, 1, 100] | None = None,
	):
		if not await require_manager(interaction):
			return
		if action == "add":
			if permission_level is None:
				await interaction.response.send_message(
					"Choose `permission_level` when adding a moderator role.", ephemeral=True,
				)
				return
			db.set_moderator_role(interaction.guild_id, role.id, permission_level)
			await interaction.response.send_message(
				f"{role.mention} is now a level {permission_level} moderator role.",
				ephemeral=True,
			)
			return
		removed = db.remove_moderator_role(interaction.guild_id, role.id)
		await interaction.response.send_message(
			f"{role.mention} was {'removed from' if removed else 'not found in'} "
			"the moderator roles.",
			ephemeral=True,
		)

	@app_commands.command(
		name="permission-level",
		description="Set the moderator permission level required by a feature.",
	)
	@app_commands.describe(
		feature="Moderation feature whose requirement should change.",
		permission_level="Required level; level 1 is highest.",
	)
	@app_commands.choices(feature=[
		app_commands.Choice(name="OCR review moderation", value=MODERATION_PERMISSION_OCR_REVIEW),
		app_commands.Choice(name="/nameless", value=MODERATION_PERMISSION_NAMELESS),
	])
	async def set_permission_level(
		self,
		interaction: discord.Interaction,
		feature: str,
		permission_level: app_commands.Range[int, 1, 100],
	):
		if not await require_manager(interaction):
			return
		db.set_moderation_permission_level(
			interaction.guild_id, feature, permission_level,
		)
		label = "OCR review moderation" if feature == MODERATION_PERMISSION_OCR_REVIEW else "/nameless"
		await interaction.response.send_message(
			f"{label} now requires moderator permission level {permission_level} "
			"or a lower-numbered level.",
			ephemeral=True,
		)

	@app_commands.command(name="timeoutlogchannel", description="Set the channel for manual timeout logs.")
	@app_commands.describe(channel="Channel that receives manual timeout logs.")
	async def set_timeout_log_channel(
		self,
		interaction: discord.Interaction,
		channel: discord.TextChannel,
	):
		if not await require_manager(interaction):
			return
		db.update_guild_settings(interaction.guild_id, timeout_log_channel_id=channel.id)
		await interaction.response.send_message(
			f"Manual timeout logs will be sent to {channel.mention}.", ephemeral=True,
		)

	@app_commands.command(name="manager", description="Add or remove roles that can configure AttachmentBot.")
	@app_commands.describe(action="Whether to add or remove the manager role.", role="The role to update.")
	@app_commands.choices(action=[
		app_commands.Choice(name="add", value="add"),
		app_commands.Choice(name="remove", value="remove"),
	])
	async def manager_roles(self, interaction: discord.Interaction, action: str, role: discord.Role):
		if not await require_admin(interaction):
			return
		if action == "add":
			db.add_manager_role(interaction.guild_id, role.id)
			await interaction.response.send_message(f"{role.mention} can now manage AttachmentBot.", ephemeral=True)
			return
		db.remove_manager_role(interaction.guild_id, role.id)
		await interaction.response.send_message(f"{role.mention} can no longer manage AttachmentBot.", ephemeral=True)


async def setup(bot):
	await bot.add_cog(AttachmentBotBaseCog(bot))
