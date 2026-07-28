import asyncio
import json

import discord
from discord import app_commands
from discord.ext import commands

from core.database import db, FEATURE_OCR_DELETE_MESSAGE, FEATURE_OCR_GIVE_ROLE
from core.feature_cog import attach_feature_cog, detach_feature_cog
from core.logger import log
from core.message_activity import message_activity
from core.permissions import require_manager
from core.queue_worker import MESSAGE_QUEUE, start_worker
from core.responses import send_json_chunks


class OcrCog(
	commands.GroupCog,
	group_name="ocr",
	group_description="Manage OCR attachment scanning.",
):
	def __init__(self, bot):
		self.bot = bot
		self.worker = None

	async def cog_load(self):
		self.worker = asyncio.create_task(start_worker(self.bot))

	async def cog_unload(self):
		detach_feature_cog(self)
		if self.worker:
			self.worker.cancel()

	@commands.Cog.listener()
	async def on_message(self, message: discord.Message):
		if message.author.bot or not message.guild:
			return
		config = db.get_ocr_settings(message.guild.id)
		if not config or not config["enabled"]:
			return
		if message.channel.id in config["channel_blacklist"]:
			return
		images = [
			attachment for attachment in message.attachments
			if attachment.content_type and attachment.content_type.startswith("image/")
		]
		previous_count = message_activity.count_before(
			message,
			config["single_image_lookback_days"],
		)
		scan_single_image = (
			len(images) == 1
			and config["single_image_max_messages"] > 0
			and previous_count is not None
			and previous_count < config["single_image_max_messages"]
		)
		if len(images) < 2 and not scan_single_image:
			return
		await MESSAGE_QUEUE.put(message)
		log.info(f"[QUEUE] message={message.id} author={message.author}")

	@app_commands.command(name="enabled", description="Enable or disable OCR scanning on this server.")
	@app_commands.describe(enabled="Whether OCR scanning should run in this server.")
	async def set_enabled(self, interaction: discord.Interaction, enabled: bool):
		if not await require_manager(interaction):
			return
		db.update_guild_settings(interaction.guild_id, enabled=enabled)
		await interaction.response.send_message(
			f"OCR scanning is now {'enabled' if enabled else 'disabled'} for this server.",
			ephemeral=True,
		)

	@app_commands.command(name="threshold", description="Set the score needed for OCR detection.")
	@app_commands.describe(points="The minimum total keyword points needed to flag a message.")
	async def set_threshold(self, interaction: discord.Interaction, points: app_commands.Range[int, 1, 100]):
		if not await require_manager(interaction):
			return
		db.update_guild_settings(interaction.guild_id, detection_threshold=points)
		await interaction.response.send_message(f"Detection threshold set to {points}.", ephemeral=True)

	@app_commands.command(name="single-image", description="Configure single-image scanning for low-activity users.")
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
		if not await require_manager(interaction):
			return
		db.update_guild_settings(
			interaction.guild_id,
			single_image_max_messages=max_messages,
			single_image_lookback_days=lookback_days,
		)
		asyncio.create_task(message_activity.initialize_ocr_guild(interaction.guild))
		state = "disabled" if max_messages == 0 else (
			f"enabled for users with fewer than {max_messages} earlier messages "
			f"in the last {lookback_days} days"
		)
		await interaction.response.send_message(f"Single-image OCR scanning is {state}.", ephemeral=True)

	@app_commands.command(name="log-channel", description="Set the channel where detections are logged.")
	@app_commands.describe(channel="The channel to receive detection logs.")
	async def set_log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
		if not await require_manager(interaction):
			return
		db.update_guild_settings(interaction.guild_id, log_channel_id=channel.id)
		await interaction.response.send_message(
			f"Detection logs will be sent to {channel.mention}.", ephemeral=True,
		)

	@app_commands.command(name="keyword", description="Add, remove, or print OCR keywords.")
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
		if not await require_manager(interaction):
			return
		if action == "print":
			await send_json_chunks(
				interaction,
				"Current keyword dictionary:",
				json.dumps(db.get_keywords(interaction.guild_id), indent=2, sort_keys=True),
			)
			return
		if not word:
			await interaction.response.send_message("Choose a keyword with `word`.", ephemeral=True)
			return
		if action == "add":
			if points is None:
				await interaction.response.send_message("Choose `points` when adding a keyword.", ephemeral=True)
				return
			parsed_aliases = [alias.strip().lower() for alias in aliases.split(",") if alias.strip()] if aliases else []
			try:
				db.add_keyword(interaction.guild_id, word, points, parsed_aliases)
			except ValueError as exc:
				await interaction.response.send_message(str(exc), ephemeral=True)
				return
			await interaction.response.send_message(
				f"Keyword `{word.strip().lower()}` saved with {points} point(s).", ephemeral=True,
			)
			return
		db.remove_keyword(interaction.guild_id, word)
		await interaction.response.send_message(
			f"Keyword `{word.strip().lower()}` removed.", ephemeral=True,
		)

	@app_commands.command(name="alias", description="Add or remove OCR keyword aliases.")
	@app_commands.describe(action="Whether to add or remove the alias.", word="The existing keyword.", alias="The alias to update.")
	@app_commands.choices(action=[
		app_commands.Choice(name="add", value="add"),
		app_commands.Choice(name="remove", value="remove"),
	])
	async def alias_config(self, interaction: discord.Interaction, action: str, word: str, alias: str):
		if not await require_manager(interaction):
			return
		if action == "add":
			try:
				db.add_alias(interaction.guild_id, word, alias)
			except ValueError as exc:
				await interaction.response.send_message(str(exc), ephemeral=True)
				return
			await interaction.response.send_message(
				f"Alias `{alias.strip().lower()}` added to `{word.strip().lower()}`.", ephemeral=True,
			)
			return
		db.remove_alias(interaction.guild_id, word, alias)
		await interaction.response.send_message(
			f"Alias `{alias.strip().lower()}` removed from `{word.strip().lower()}`.", ephemeral=True,
		)

	@app_commands.command(name="blacklist", description="Add or remove channels from OCR scanning blacklist.")
	@app_commands.describe(action="Whether to add or remove the channel.", channel="The channel to update.")
	@app_commands.choices(action=[
		app_commands.Choice(name="add", value="add"),
		app_commands.Choice(name="remove", value="remove"),
	])
	async def blacklist_config(self, interaction: discord.Interaction, action: str, channel: discord.TextChannel):
		if not await require_manager(interaction):
			return
		if action == "add":
			db.add_blacklisted_channel(interaction.guild_id, channel.id)
			await interaction.response.send_message(f"{channel.mention} is now blacklisted.", ephemeral=True)
			return
		db.remove_blacklisted_channel(interaction.guild_id, channel.id)
		await interaction.response.send_message(f"{channel.mention} is no longer blacklisted.", ephemeral=True)

	@app_commands.command(name="moderation", description="Set OCR moderation actions.")
	@app_commands.describe(action="The OCR moderation action to update.", enabled="Whether the action should be enabled.")
	@app_commands.choices(action=[
		app_commands.Choice(name="give-role", value="give-role"),
		app_commands.Choice(name="delete-message", value="delete-message"),
	])
	async def moderation_config(self, interaction: discord.Interaction, action: str, enabled: bool):
		if not await require_manager(interaction):
			return
		if action == "give-role":
			db.set_feature_enabled(interaction.guild_id, FEATURE_OCR_GIVE_ROLE, enabled)
			await interaction.response.send_message(
				f"Role moderation is now {'enabled' if enabled else 'disabled'}.", ephemeral=True,
			)
			return
		db.set_feature_enabled(interaction.guild_id, FEATURE_OCR_DELETE_MESSAGE, enabled)
		await interaction.response.send_message(
			f"Message deletion is now {'enabled' if enabled else 'disabled'}.", ephemeral=True,
		)


async def setup(bot):
	await attach_feature_cog(bot, OcrCog(bot))
