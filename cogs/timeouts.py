from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from core.database import db, MODERATION_PERMISSION_NAMELESS
from core.logger import log
from core.permissions import require_moderator
from core.temporary_roles import assign_temporary_role


NO_MENTIONS = discord.AllowedMentions.none()


async def resolve_channel(bot, channel_id: int):
	channel = bot.get_channel(channel_id)
	if channel:
		return channel
	try:
		return await bot.fetch_channel(channel_id)
	except discord.DiscordException:
		return None


def validate_timeout_hierarchy(
	guild: discord.Guild,
	actor: discord.Member,
	target: discord.Member,
	timeout_role: discord.Role,
):
	bot_member = guild.me
	if not bot_member:
		raise ValueError("The bot could not resolve its server role hierarchy.")
	if not bot_member.guild_permissions.manage_roles:
		raise ValueError("The bot does not have the Manage Roles permission.")
	if target.id == guild.owner_id:
		raise ValueError("The server owner cannot be timed out.")
	if actor.id != guild.owner_id and target.top_role >= actor.top_role:
		raise ValueError("You cannot time out a member with an equal or higher role.")
	if actor.id != guild.owner_id and timeout_role >= actor.top_role:
		raise ValueError("Your highest role must be above the configured timeout role.")
	if target.top_role >= bot_member.top_role:
		raise ValueError("The bot's highest role must be above the target member's highest role.")
	if timeout_role >= bot_member.top_role:
		raise ValueError("The bot's highest role must be above the configured timeout role.")


@app_commands.guild_only()
class TimeoutCog(commands.Cog):
	def __init__(self, bot):
		self.bot = bot

	@app_commands.command(name="nameless", description="Give a member the configured timeout role.")
	@app_commands.describe(
		member="Member to time out.",
		duration_minutes="Duration in minutes, or -1 for an indefinite timeout.",
		reason="Reason recorded in the timeout log.",
	)
	async def timeout_member(
		self,
		interaction: discord.Interaction,
		member: discord.Member,
		duration_minutes: app_commands.Range[int, -1, 525600],
		reason: app_commands.Range[str, 1, 512],
	):
		required_level = db.get_moderation_permission_level(
			interaction.guild_id, MODERATION_PERMISSION_NAMELESS,
		)
		if not await require_moderator(interaction, required_level):
			return
		if duration_minutes == 0:
			await interaction.response.send_message(
				"Duration must be at least 1 minute, or -1 for indefinite.", ephemeral=True,
			)
			return

		settings = db.get_shared_settings(interaction.guild_id)
		role_id = settings["role_id"] if settings else None
		log_channel_id = settings["timeout_log_channel_id"] if settings else None
		timeout_role = interaction.guild.get_role(role_id) if role_id else None
		if not timeout_role:
			await interaction.response.send_message(
				"Set the timeout role with `/ab timeout-role` first.", ephemeral=True,
			)
			return
		if not log_channel_id:
			await interaction.response.send_message(
				"Set the timeout log channel with `/ab timeoutlogchannel` first.", ephemeral=True,
			)
			return
		log_channel = await resolve_channel(self.bot, log_channel_id)
		if not log_channel:
			await interaction.response.send_message(
				"The configured timeout log channel is unavailable to the bot.", ephemeral=True,
			)
			return
		try:
			validate_timeout_hierarchy(
				interaction.guild, interaction.user, member, timeout_role,
			)
		except ValueError as exc:
			await interaction.response.send_message(str(exc), ephemeral=True)
			return
		log_permissions = log_channel.permissions_for(interaction.guild.me)
		if not log_permissions.send_messages or not log_permissions.embed_links:
			await interaction.response.send_message(
				"The bot needs Send Messages and Embed Links in the timeout log channel.",
				ephemeral=True,
			)
			return

		await interaction.response.defer(ephemeral=True, thinking=True)
		duration_seconds = -1 if duration_minutes == -1 else duration_minutes * 60
		audit_reason = (
			f"Timeout by {interaction.user} ({interaction.user.id}): {reason}"
		)[:512]
		try:
			await assign_temporary_role(
				member, timeout_role, duration_seconds, audit_reason,
			)
		except discord.DiscordException as exc:
			await interaction.followup.send(
				f"Could not apply the timeout role: {exc}", ephemeral=True,
			)
			return

		if duration_minutes == -1:
			duration_text = "Indefinite"
		else:
			expires_at = int(datetime.now(timezone.utc).timestamp()) + duration_seconds
			duration_text = (
				f"{duration_minutes} minute{'s' if duration_minutes != 1 else ''} "
				f"(expires <t:{expires_at}:R>)"
			)
		embed = discord.Embed(title="Member Timed Out", color=discord.Color.orange())
		embed.add_field(name="Member", value=f"{member.mention} (`{member.id}`)", inline=False)
		embed.add_field(
			name="Moderator",
			value=f"{interaction.user.mention} (`{interaction.user.id}`)",
			inline=False,
		)
		embed.add_field(name="Duration", value=duration_text, inline=False)
		embed.add_field(name="Reason", value=reason, inline=False)
		logged = True
		try:
			await log_channel.send(embed=embed, allowed_mentions=NO_MENTIONS)
		except discord.DiscordException as exc:
			logged = False
			log.info(
				f"[TIMEOUT LOG ERROR] guild={interaction.guild_id} user={member.id} error={exc}"
			)

		confirmation = f"Applied {timeout_role.mention} to {member.mention}."
		if not logged:
			confirmation += " The timeout was applied, but the log message could not be sent."
		await interaction.followup.send(
			confirmation,
			ephemeral=True,
			allowed_mentions=NO_MENTIONS,
		)


async def setup(bot):
	await bot.add_cog(TimeoutCog(bot))
