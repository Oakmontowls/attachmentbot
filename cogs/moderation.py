import discord
import json
import asyncio
from discord import app_commands
from discord.ext import commands
from core.queue_worker import MESSAGE_QUEUE
from core.logger import log
from core.database import db, FEATURE_OCR_DELETE_MESSAGE, FEATURE_OCR_GIVE_ROLE
from core.pressure import pressure_moderator
from core.message_activity import message_activity

class ModerationCog(commands.Cog):
	def __init__(self, bot):
		self.bot = bot

	def get_guild_config(self, guild_id: int):
		return db.get_guild_settings(guild_id)

	def is_admin(self, member: discord.Member) -> bool:
		return member.guild_permissions.administrator

	def can_manage_bot(self, member: discord.Member) -> bool:
		if self.is_admin(member):
			return True

		manager_roles = db.get_manager_roles(member.guild.id)
		return any(role.id in manager_roles for role in member.roles)

	async def require_admin(self, interaction: discord.Interaction) -> bool:
		if interaction.guild and isinstance(interaction.user, discord.Member) and self.is_admin(interaction.user):
			return True

		await interaction.response.send_message(
			"Only server administrators can use this command.",
			ephemeral=True,
		)
		return False

	async def require_manager(self, interaction: discord.Interaction) -> bool:
		if interaction.guild and isinstance(interaction.user, discord.Member) and self.can_manage_bot(interaction.user):
			return True

		await interaction.response.send_message(
			"You need Administrator or an approved manager role to change this bot's setup.",
			ephemeral=True,
		)
		return False

	def parse_aliases(self, aliases: str | None) -> list[str]:
		if not aliases:
			return []
		return [alias.strip().lower() for alias in aliases.split(",") if alias.strip()]

	async def send_code_chunks(self, interaction: discord.Interaction, title: str, content: str):
		chunk_size = 1800
		chunks = [content[i:i + chunk_size] for i in range(0, len(content), chunk_size)] or ["{}"]

		await interaction.response.send_message(
			f"{title}\n```json\n{chunks[0]}\n```",
			ephemeral=True,
		)

		for chunk in chunks[1:]:
			await interaction.followup.send(
				f"```json\n{chunk}\n```",
				ephemeral=True,
			)

	@commands.Cog.listener()
	async def on_message(self, message: discord.Message):

		if message.author.bot:
			return

		if not message.guild:
			return

		config = self.get_guild_config(message.guild.id)
		if not config:
			return

		previous_message_count = message_activity.count(
			message.guild.id,
			message.author.id,
			config["single_image_lookback_days"],
		)
		message_activity.record_live(message)

		if message.channel.id in config["channel_blacklist"]:
			return

		pressure_triggered = await pressure_moderator.process_message(self.bot, message, config)
		if pressure_triggered:
			return

		if not config["enabled"]:
			return

		images = [
			a for a in message.attachments
			if a.content_type and a.content_type.startswith("image/")
		]

		scan_single_image = (
			len(images) == 1
			and config["single_image_max_messages"] > 0
			and previous_message_count is not None
			and previous_message_count < config["single_image_max_messages"]
		)
		if len(images) < 2 and not scan_single_image:
			return

		await MESSAGE_QUEUE.put(message)

		log.info(f"[QUEUE] message={message.id} author={message.author}")

	attachmentbot = app_commands.Group(
		name="ab",
		description="Configure AttachmentBot for this server.",
		guild_only=True,
	)

	pressure = app_commands.Group(
		name="pressure",
		description="Manage pressure-based moderation.",
		parent=attachmentbot,
	)

	ocr = app_commands.Group(
		name="ocr",
		description="Manage OCR attachment scanning.",
		parent=attachmentbot,
	)

	@attachmentbot.command(name="status", description="Show this server's AttachmentBot setup.")
	async def status(self, interaction: discord.Interaction):
		if not await self.require_manager(interaction):
			return

		config = db.get_guild_settings(interaction.guild_id)
		if not config:
			await interaction.response.send_message("This server has not been configured yet.", ephemeral=True)
			return

		manager_roles = ", ".join(f"<@&{role_id}>" for role_id in sorted(config["manager_roles"])) or "None"
		blacklisted = ", ".join(f"<#{channel_id}>" for channel_id in sorted(config["channel_blacklist"])) or "None"
		features = config["features"]
		log_channel = f"<#{config['log_channel_id']}>" if config["log_channel_id"] else "Not set"
		pressure_log_channel = (
			f"<#{config['pressure']['log_channel_id']}>"
			if config["pressure"]["log_channel_id"]
			else f"Fallback to OCR log channel ({log_channel})"
		)
		timeout_role = f"<@&{config['role_id']}>" if config["role_id"] else "Not set"

		embed = discord.Embed(
			title="AttachmentBot Status",
			color=discord.Color.blurple(),
		)
		embed.add_field(name="Enabled", value=str(config["enabled"]), inline=True)
		embed.add_field(name="Threshold", value=str(config["detection_threshold"]), inline=True)
		embed.add_field(
			name="Single Image Scan",
			value=(
				f"Fewer than {config['single_image_max_messages']} messages in "
				f"{config['single_image_lookback_days']} days"
				if config["single_image_max_messages"] > 0 else "Disabled"
			),
			inline=False,
		)
		embed.add_field(name="Log Channel", value=log_channel, inline=False)
		embed.add_field(name="Timeout Role", value=timeout_role, inline=False)
		embed.add_field(name="Give Role", value=str(features.get(FEATURE_OCR_GIVE_ROLE, False)), inline=True)
		embed.add_field(name="Delete Message", value=str(features.get(FEATURE_OCR_DELETE_MESSAGE, False)), inline=True)
		embed.add_field(name="Pressure Enabled", value=str(config["pressure"]["enabled"]), inline=True)
		embed.add_field(name="Pressure Threshold", value=str(config["pressure"]["threshold"]), inline=True)
		embed.add_field(name="Pressure Decay", value=f"{config['pressure']['decay_per_second']}/sec", inline=True)
		embed.add_field(name="Pressure Role Duration", value=f"{config['pressure']['role_duration_seconds']}s", inline=True)
		embed.add_field(name="Pressure Log Channel", value=pressure_log_channel, inline=False)
		embed.add_field(name="Manager Roles", value=manager_roles[:1024], inline=False)
		embed.add_field(name="Blacklisted Channels", value=blacklisted[:1024], inline=False)
		embed.add_field(name="Keywords", value=str(len(config["keywords"])), inline=True)

		await interaction.response.send_message(embed=embed, ephemeral=True)

	@ocr.command(name="enabled", description="Enable or disable OCR scanning on this server.")
	@app_commands.describe(enabled="Whether OCR scanning should run in this server.")
	async def set_enabled(self, interaction: discord.Interaction, enabled: bool):
		if not await self.require_manager(interaction):
			return

		db.update_guild_settings(interaction.guild_id, enabled=enabled)
		await interaction.response.send_message(f"AttachmentBot is now {'enabled' if enabled else 'disabled'} for this server.", ephemeral=True)

	@ocr.command(name="threshold", description="Set the score needed for OCR detection.")
	@app_commands.describe(points="The minimum total keyword points needed to flag a message.")
	async def set_threshold(self, interaction: discord.Interaction, points: app_commands.Range[int, 1, 100]):
		if not await self.require_manager(interaction):
			return

		db.update_guild_settings(interaction.guild_id, detection_threshold=points)
		await interaction.response.send_message(f"Detection threshold set to {points}.", ephemeral=True)

	@ocr.command(name="single-image", description="Configure single-image scanning for low-activity users.")
	@app_commands.describe(
		max_messages="Scan when the user has fewer earlier messages than this; 0 disables it.",
		lookback_days="Number of days of message history to count.",
	)
	async def set_single_image_scanning(
		self,
		interaction: discord.Interaction,
		max_messages: app_commands.Range[int, 0, 100000],
		lookback_days: app_commands.Range[int, 1, 365],
	):
		if not await self.require_manager(interaction):
			return

		db.update_guild_settings(
			interaction.guild_id,
			single_image_max_messages=max_messages,
			single_image_lookback_days=lookback_days,
		)
		asyncio.create_task(message_activity.initialize_guild(interaction.guild))
		state = "disabled" if max_messages == 0 else (
			f"enabled for users with fewer than {max_messages} earlier messages "
			f"in the last {lookback_days} days"
		)
		await interaction.response.send_message(f"Single-image OCR scanning is {state}.", ephemeral=True)

	@ocr.command(name="log-channel", description="Set the channel where detections are logged.")
	@app_commands.describe(channel="The channel to receive detection logs.")
	async def set_log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
		if not await self.require_manager(interaction):
			return

		db.update_guild_settings(interaction.guild_id, log_channel_id=channel.id)
		await interaction.response.send_message(f"Detection logs will be sent to {channel.mention}.", ephemeral=True)

	@attachmentbot.command(name="timeout-role", description="Set the timeout role given by moderation features.")
	@app_commands.describe(role="The timeout role to give to detected users.")
	async def set_timeout_role(self, interaction: discord.Interaction, role: discord.Role):
		if not await self.require_manager(interaction):
			return

		db.update_guild_settings(interaction.guild_id, role_id=role.id)
		await interaction.response.send_message(f"Timeout role set to {role.mention}.", ephemeral=True)

	@attachmentbot.command(name="manager", description="Add or remove roles that can configure AttachmentBot.")
	@app_commands.describe(
		action="Whether to add or remove the manager role.",
		role="The role to update.",
	)
	@app_commands.choices(action=[
		app_commands.Choice(name="add", value="add"),
		app_commands.Choice(name="remove", value="remove"),
	])
	async def manager_roles(self, interaction: discord.Interaction, action: str, role: discord.Role):
		if not await self.require_admin(interaction):
			return

		if action == "add":
			db.add_manager_role(interaction.guild_id, role.id)
			await interaction.response.send_message(f"{role.mention} can now manage AttachmentBot.", ephemeral=True)
			return

		db.remove_manager_role(interaction.guild_id, role.id)
		await interaction.response.send_message(f"{role.mention} can no longer manage AttachmentBot.", ephemeral=True)

	@ocr.command(name="keyword", description="Add, remove, or print OCR keywords.")
	@app_commands.describe(
		action="What to do with the keyword dictionary.",
		word="The keyword to add or remove.",
		points="How many points this keyword contributes when adding.",
		aliases="Optional comma-separated aliases when adding.",
	)
	@app_commands.choices(action=[
		app_commands.Choice(name="add", value="add"),
		app_commands.Choice(name="remove", value="remove"),
		app_commands.Choice(name="print", value="print"),
	])
	async def keyword_config(
		self,
		interaction: discord.Interaction,
		action: str,
		word: str | None = None,
		points: app_commands.Range[int, 1, 100] | None = None,
		aliases: str | None = None,
	):
		if not await self.require_manager(interaction):
			return

		if action == "print":
			keywords = db.get_keywords(interaction.guild_id)
			formatted = json.dumps(keywords, indent=2, sort_keys=True)
			await self.send_code_chunks(interaction, "Current keyword dictionary:", formatted)
			return

		if not word:
			await interaction.response.send_message("Choose a keyword with `word`.", ephemeral=True)
			return

		if action == "add":
			if points is None:
				await interaction.response.send_message("Choose `points` when adding a keyword.", ephemeral=True)
				return

			try:
				db.add_keyword(interaction.guild_id, word, points, self.parse_aliases(aliases))
			except ValueError as exc:
				await interaction.response.send_message(str(exc), ephemeral=True)
				return

			await interaction.response.send_message(f"Keyword `{word.strip().lower()}` saved with {points} point(s).", ephemeral=True)
			return

		db.remove_keyword(interaction.guild_id, word)
		await interaction.response.send_message(f"Keyword `{word.strip().lower()}` removed.", ephemeral=True)

	@ocr.command(name="alias", description="Add or remove OCR keyword aliases.")
	@app_commands.describe(
		action="Whether to add or remove the alias.",
		word="The existing keyword.",
		alias="The alias to add or remove.",
	)
	@app_commands.choices(action=[
		app_commands.Choice(name="add", value="add"),
		app_commands.Choice(name="remove", value="remove"),
	])
	async def alias_config(self, interaction: discord.Interaction, action: str, word: str, alias: str):
		if not await self.require_manager(interaction):
			return

		if action == "add":
			try:
				db.add_alias(interaction.guild_id, word, alias)
			except ValueError as exc:
				await interaction.response.send_message(str(exc), ephemeral=True)
				return

			await interaction.response.send_message(f"Alias `{alias.strip().lower()}` added to `{word.strip().lower()}`.", ephemeral=True)
			return

		db.remove_alias(interaction.guild_id, word, alias)
		await interaction.response.send_message(f"Alias `{alias.strip().lower()}` removed from `{word.strip().lower()}`.", ephemeral=True)

	@ocr.command(name="blacklist", description="Add or remove channels from OCR scanning blacklist.")
	@app_commands.describe(
		action="Whether to add or remove the channel.",
		channel="The channel to update.",
	)
	@app_commands.choices(action=[
		app_commands.Choice(name="add", value="add"),
		app_commands.Choice(name="remove", value="remove"),
	])
	async def blacklist_config(self, interaction: discord.Interaction, action: str, channel: discord.TextChannel):
		if not await self.require_manager(interaction):
			return

		if action == "add":
			db.add_blacklisted_channel(interaction.guild_id, channel.id)
			await interaction.response.send_message(f"{channel.mention} is now blacklisted.", ephemeral=True)
			return

		db.remove_blacklisted_channel(interaction.guild_id, channel.id)
		await interaction.response.send_message(f"{channel.mention} is no longer blacklisted.", ephemeral=True)

	@ocr.command(name="moderation", description="Set OCR moderation actions.")
	@app_commands.describe(
		action="The OCR moderation action to update.",
		enabled="Whether the action should be enabled.",
	)
	@app_commands.choices(action=[
		app_commands.Choice(name="give-role", value="give-role"),
		app_commands.Choice(name="delete-message", value="delete-message"),
	])
	async def moderation_config(self, interaction: discord.Interaction, action: str, enabled: bool):
		if not await self.require_manager(interaction):
			return

		if action == "give-role":
			db.set_feature_enabled(interaction.guild_id, FEATURE_OCR_GIVE_ROLE, enabled)
			await interaction.response.send_message(f"Role moderation is now {'enabled' if enabled else 'disabled'}.", ephemeral=True)
			return

		db.set_feature_enabled(interaction.guild_id, FEATURE_OCR_DELETE_MESSAGE, enabled)
		await interaction.response.send_message(f"Message deletion is now {'enabled' if enabled else 'disabled'}.", ephemeral=True)

	@pressure.command(name="status", description="Show pressure moderation settings.")
	async def pressure_status(self, interaction: discord.Interaction):
		if not await self.require_manager(interaction):
			return

		settings = db.get_pressure_settings(interaction.guild_id)
		current = dict(settings)
		current["channel_thresholds"] = {
			str(channel_id): threshold
			for channel_id, threshold in settings["channel_thresholds"].items()
		}
		formatted = json.dumps(current, indent=2, sort_keys=True)
		await self.send_code_chunks(interaction, "Current pressure settings:", formatted)

	@pressure.command(name="view", description="View a member's current pressure.")
	@app_commands.describe(member="The member whose pressure should be shown.")
	async def pressure_view(self, interaction: discord.Interaction, member: discord.Member):
		if not await self.require_manager(interaction):
			return

		settings = db.get_pressure_settings(interaction.guild_id)
		channel_pressures = pressure_moderator.current_channel_pressures_for_guild(interaction.guild, member.id)
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

	@pressure.command(name="set", description="Set pressure moderation options.")
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
		delete_message="Delete messages posted while the user is already over the pressure threshold.",
		give_role="Give the configured timeout role.",
		role_duration_minutes="Minutes before the pressure timeout role is removed. Use 0 for permanent.",
		log_channel="Channel for pressure moderation logs. Leave every option blank to fall back to OCR logs.",
		channel="Channel for a threshold override. Omit channel_threshold to remove the override.",
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
		if not await self.require_manager(interaction):
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
				await interaction.response.send_message("Choose a channel when setting a channel threshold.", ephemeral=True)
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

	@pressure.command(name="reset", description="Clear tracked pressure for a user or the whole server.")
	@app_commands.describe(member="Optional member whose pressure should be cleared.")
	async def pressure_reset(self, interaction: discord.Interaction, member: discord.Member | None = None):
		if not await self.require_manager(interaction):
			return

		if member:
			pressure_moderator.reset_user(interaction.guild_id, member.id)
			await interaction.response.send_message(f"Cleared pressure for {member.mention}.", ephemeral=True)
			return

		pressure_moderator.reset_guild(interaction.guild_id)
		await interaction.response.send_message("Cleared pressure for this server.", ephemeral=True)

	@pressure.command(name="channel-thresholds", description="List channel-specific pressure thresholds.")
	async def pressure_channel_threshold_list(self, interaction: discord.Interaction):
		if not await self.require_manager(interaction):
			return

		thresholds = db.get_pressure_channel_thresholds(interaction.guild_id)
		if not thresholds:
			await interaction.response.send_message("No channel-specific pressure thresholds are set.", ephemeral=True)
			return

		lines = [
			f"<#{channel_id}>: {threshold:g}"
			for channel_id, threshold in sorted(thresholds.items())
		]
		await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

	@pressure.command(name="banned-word", description="Add, remove, or print pressure banned words.")
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
		if not await self.require_manager(interaction):
			return

		if action == "print":
			words = db.get_pressure_banned_words(interaction.guild_id)
			formatted = json.dumps(words, indent=2, sort_keys=True)
			await self.send_code_chunks(interaction, "Current pressure banned words:", formatted)
			return

		if not word:
			await interaction.response.send_message("Choose a regex entry or pipe-separated list with `word`.", ephemeral=True)
			return

		if action == "set":
			words = [entry.strip() for entry in word.split("|") if entry.strip()]
			try:
				db.set_pressure_banned_words(interaction.guild_id, words)
			except ValueError as exc:
				await interaction.response.send_message(str(exc), ephemeral=True)
				return
			await interaction.response.send_message(f"Pressure banned-word regex list replaced with {len(words)} entrie(s).", ephemeral=True)
			return

		if action == "add":
			try:
				db.add_pressure_banned_word(interaction.guild_id, word)
			except ValueError as exc:
				await interaction.response.send_message(str(exc), ephemeral=True)
				return
			await interaction.response.send_message(f"Pressure banned-word regex `{word.strip()}` added.", ephemeral=True)
			return

		db.remove_pressure_banned_word(interaction.guild_id, word)
		await interaction.response.send_message(f"Pressure banned-word regex `{word.strip()}` removed.", ephemeral=True)

async def setup(bot):
	await bot.add_cog(ModerationCog(bot))
