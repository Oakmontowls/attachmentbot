import json

import discord
from discord import app_commands
from discord.ext import commands

from core.database import db
from core.feature_cog import attach_feature_cog, detach_feature_cog
from core.permissions import require_manager
from core.pressure import pressure_moderator
from core.responses import send_json_chunks


class PressureCog(
	commands.GroupCog,
	group_name="pressure",
	group_description="Manage pressure-based moderation.",
):
	def __init__(self, bot):
		self.bot = bot

	async def cog_unload(self):
		detach_feature_cog(self)

	@commands.Cog.listener()
	async def on_message(self, message: discord.Message):
		if message.author.bot or not message.guild:
			return
		shared = db.get_shared_settings(message.guild.id)
		if not shared:
			return
		config = {**shared, "pressure": db.get_pressure_settings(message.guild.id)}
		await pressure_moderator.process_message(self.bot, message, config)

	@app_commands.command(name="status", description="Show pressure moderation settings.")
	async def pressure_status(self, interaction: discord.Interaction):
		if not await require_manager(interaction):
			return
		settings = db.get_pressure_settings(interaction.guild_id)
		current = dict(settings)
		current["channel_thresholds"] = {
			str(channel_id): threshold
			for channel_id, threshold in settings["channel_thresholds"].items()
		}
		await send_json_chunks(
			interaction,
			"Current pressure settings:",
			json.dumps(current, indent=2, sort_keys=True),
		)

	@app_commands.command(name="view", description="View a member's current pressure.")
	@app_commands.describe(member="The member whose pressure should be shown.")
	async def pressure_view(self, interaction: discord.Interaction, member: discord.Member):
		if not await require_manager(interaction):
			return
		settings = db.get_pressure_settings(interaction.guild_id)
		channel_pressures = pressure_moderator.current_channel_pressures_for_guild(
			interaction.guild, member.id
		)
		pressure_text = "None"
		if channel_pressures:
			pressure_text = "\n".join(
				f"<#{channel_id}>: {current}/{threshold}"
				for channel_id, current, threshold in channel_pressures
			)
		embed = discord.Embed(
			title="Pressure Status",
			color=discord.Color.orange(),
			description=f"Member: {member.mention}",
		)
		embed.add_field(name="Pressure Enabled", value=str(settings["enabled"]), inline=True)
		embed.add_field(name="Decay", value=f"{settings['decay_per_second']:g}/sec", inline=True)
		embed.add_field(name="Channel Pressure", value=pressure_text[:1024], inline=False)
		await interaction.response.send_message(embed=embed, ephemeral=True)

	@app_commands.command(name="set", description="Set pressure moderation options.")
	@app_commands.describe(
		enabled="Whether pressure moderation should run.",
		threshold="Global pressure threshold. Default: 100.",
		decay="Pressure points removed per second. Default: 3.3.",
		base="Pressure added by every message.",
		attachment="Pressure added per image.",
		embed="Pressure added per embed.",
		mention="Pressure added per mention.",
		link="Pressure added when a message contains a link.",
		duplicate="Pressure added when content repeats the user's previous message.",
		line="Pressure added per newline.",
		solo_emote="Pressure added when a message is only an emote.",
		gif="Pressure added per GIF.",
		banned_word="Pressure added per configured banned word hit.",
		new_member="Pressure added while the user is within the new-member window.",
		new_member_hours="Hours after joining where new-member pressure applies.",
		delete_message="Delete messages posted while the user is over the threshold.",
		give_role="Give the configured timeout role.",
		role_duration_minutes="Minutes before the timeout role is removed. Use 0 for permanent.",
		log_channel="Channel for pressure moderation logs.",
		channel="Channel for a threshold override. Omit channel_threshold to remove it.",
		channel_threshold="Whole-number override threshold for the selected channel.",
	)
	async def pressure_set(
		self,
		interaction: discord.Interaction,
		enabled: bool | None = None,
		threshold: app_commands.Range[int, 1, 10000] | None = None,
		decay: app_commands.Range[float, 0.0, 10000.0] | None = None,
		base: app_commands.Range[int, 0, 10000] | None = None,
		attachment: app_commands.Range[int, 0, 10000] | None = None,
		embed: app_commands.Range[int, 0, 10000] | None = None,
		mention: app_commands.Range[int, 0, 10000] | None = None,
		link: app_commands.Range[int, 0, 10000] | None = None,
		duplicate: app_commands.Range[int, 0, 10000] | None = None,
		line: app_commands.Range[int, 0, 10000] | None = None,
		solo_emote: app_commands.Range[int, 0, 10000] | None = None,
		gif: app_commands.Range[int, 0, 10000] | None = None,
		banned_word: app_commands.Range[int, 0, 10000] | None = None,
		new_member: app_commands.Range[int, 0, 10000] | None = None,
		new_member_hours: app_commands.Range[int, 0, 8760] | None = None,
		delete_message: bool | None = None,
		give_role: bool | None = None,
		role_duration_minutes: app_commands.Range[int, 0, 10080] | None = None,
		log_channel: discord.TextChannel | None = None,
		channel: discord.TextChannel | discord.ForumChannel | None = None,
		channel_threshold: app_commands.Range[int, 1, 10000] | None = None,
	):
		if not await require_manager(interaction):
			return
		updates = {
			"enabled": enabled,
			"threshold": threshold,
			"decay_per_second": decay,
			"base_pressure": base,
			"attachment_pressure": attachment,
			"embed_pressure": embed,
			"mention_pressure": mention,
			"link_pressure": link,
			"duplicate_pressure": duplicate,
			"line_pressure": line,
			"solo_emote_pressure": solo_emote,
			"gif_pressure": gif,
			"banned_word_pressure": banned_word,
			"new_member_pressure": new_member,
			"new_member_hours": new_member_hours,
			"delete_message": delete_message,
			"give_role": give_role,
			"role_duration_seconds": role_duration_minutes * 60 if role_duration_minutes is not None else None,
		}
		updates = {key: value for key, value in updates.items() if value is not None}
		changes = []
		if updates:
			db.update_pressure_settings(interaction.guild_id, **updates)
			changes.extend(key.replace("_", " ") for key in updates)
		if log_channel:
			db.update_pressure_settings(interaction.guild_id, log_channel_id=log_channel.id)
			changes.append(f"pressure log channel {log_channel.mention}")
		if enabled is False:
			pressure_moderator.reset_guild(interaction.guild_id)
		if channel_threshold is not None:
			if not channel:
				await interaction.response.send_message(
					"Choose a channel when setting a channel threshold.", ephemeral=True,
				)
				return
			db.set_pressure_channel_threshold(interaction.guild_id, channel.id, channel_threshold)
			changes.append(f"{channel.mention} threshold")
		elif channel:
			db.remove_pressure_channel_threshold(interaction.guild_id, channel.id)
			changes.append(f"removed {channel.mention} override")
		if not changes:
			await interaction.response.send_message("No pressure settings were changed.", ephemeral=True)
			return
		await interaction.response.send_message(f"Updated: {', '.join(changes)}.", ephemeral=True)

	@app_commands.command(name="reset", description="Clear tracked pressure for a user or the whole server.")
	@app_commands.describe(member="Optional member whose pressure should be cleared.")
	async def pressure_reset(self, interaction: discord.Interaction, member: discord.Member | None = None):
		if not await require_manager(interaction):
			return
		if member:
			pressure_moderator.reset_user(interaction.guild_id, member.id)
			await interaction.response.send_message(f"Cleared pressure for {member.mention}.", ephemeral=True)
			return
		pressure_moderator.reset_guild(interaction.guild_id)
		await interaction.response.send_message("Cleared pressure for this server.", ephemeral=True)

	@app_commands.command(name="channel-thresholds", description="List channel-specific pressure thresholds.")
	async def pressure_channel_threshold_list(self, interaction: discord.Interaction):
		if not await require_manager(interaction):
			return
		thresholds = db.get_pressure_channel_thresholds(interaction.guild_id)
		if not thresholds:
			await interaction.response.send_message("No channel-specific pressure thresholds are set.", ephemeral=True)
			return
		lines = [f"<#{channel_id}>: {threshold:g}" for channel_id, threshold in sorted(thresholds.items())]
		await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

	@app_commands.command(name="banned-word", description="Add, remove, or print pressure banned words.")
	@app_commands.describe(
		action="What to do with the pressure banned-word list.",
		word="Regex entry to add/remove, or full pipe-separated regex list for set.",
	)
	@app_commands.choices(action=[
		app_commands.Choice(name="add", value="add"),
		app_commands.Choice(name="remove", value="remove"),
		app_commands.Choice(name="set", value="set"),
		app_commands.Choice(name="print", value="print"),
	])
	async def pressure_banned_word(self, interaction: discord.Interaction, action: str, word: str | None = None):
		if not await require_manager(interaction):
			return
		if action == "print":
			await send_json_chunks(
				interaction,
				"Current pressure banned words:",
				json.dumps(db.get_pressure_banned_words(interaction.guild_id), indent=2, sort_keys=True),
			)
			return
		if not word:
			await interaction.response.send_message(
				"Choose a regex entry or pipe-separated list with `word`.", ephemeral=True,
			)
			return
		if action == "set":
			words = [entry.strip() for entry in word.split("|") if entry.strip()]
			try:
				db.set_pressure_banned_words(interaction.guild_id, words)
			except ValueError as exc:
				await interaction.response.send_message(str(exc), ephemeral=True)
				return
			await interaction.response.send_message(
				f"Pressure banned-word regex list replaced with {len(words)} entrie(s).", ephemeral=True,
			)
			return
		if action == "add":
			try:
				db.add_pressure_banned_word(interaction.guild_id, word)
			except ValueError as exc:
				await interaction.response.send_message(str(exc), ephemeral=True)
				return
			await interaction.response.send_message(
				f"Pressure banned-word regex `{word.strip()}` added.", ephemeral=True,
			)
			return
		db.remove_pressure_banned_word(interaction.guild_id, word)
		await interaction.response.send_message(
			f"Pressure banned-word regex `{word.strip()}` removed.", ephemeral=True,
		)


async def setup(bot):
	await attach_feature_cog(bot, PressureCog(bot))
