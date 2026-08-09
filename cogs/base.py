import discord
from discord import app_commands
from discord.ext import commands

from core.database import db, FEATURE_OCR_DELETE_MESSAGE, FEATURE_OCR_GIVE_ROLE
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
		managers = ", ".join(f"<@&{role_id}>" for role_id in sorted(config["manager_roles"])) or "None"
		embed.add_field(name="Timeout Role", value=timeout_role, inline=False)
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
