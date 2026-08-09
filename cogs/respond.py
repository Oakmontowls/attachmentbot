import json
import re

import discord
from discord import app_commands
from discord.ext import commands

from core.database import db
from core.feature_cog import attach_feature_cog, detach_feature_cog
from core.permissions import require_manager
from core.respond import respond_service
from core.responses import send_json_chunks


def validate_regex(trigger: str, is_regex: bool):
	if not is_regex:
		return
	try:
		re.compile(trigger, re.IGNORECASE)
	except re.error as exc:
		raise ValueError(f"Invalid regular expression: {exc}") from exc


def parse_sticker_id(value: str) -> int:
	if not value.isdecimal():
		raise ValueError("Sticker ID must contain only numbers.")
	sticker_id = int(value)
	if sticker_id < 1 or sticker_id > 9223372036854775807:
		raise ValueError("Sticker ID is outside the supported Discord ID range.")
	return sticker_id


class RespondCog(
	commands.GroupCog,
	group_name="respond",
	group_description="Configure automatic message responses.",
):
	def __init__(self, bot):
		self.bot = bot

	async def cog_load(self):
		respond_service.start(self.bot)

	async def cog_unload(self):
		detach_feature_cog(self)
		respond_service.stop()

	@commands.Cog.listener()
	async def on_message(self, message: discord.Message):
		await respond_service.handle_message(message)

	@app_commands.command(name="enabled", description="Enable or disable automatic responses.")
	@app_commands.describe(enabled="Whether configured responses should be sent.")
	async def respond_enabled(self, interaction: discord.Interaction, enabled: bool):
		if not await require_manager(interaction):
			return
		db.set_respond_enabled(interaction.guild_id, enabled)
		await interaction.response.send_message(
			f"Automatic responses are now {'enabled' if enabled else 'disabled'}.",
			ephemeral=True,
		)

	@app_commands.command(name="add", description="Create a response with its first trigger.")
	@app_commands.describe(
		response="Message the bot should send.",
		trigger="Text or regular expression that activates the response.",
		chance="Chance to respond, from 0 through 1.",
		cooldown_seconds="Seconds before this trigger can respond again.",
		regex="Treat the trigger as a regular expression.",
	)
	async def respond_add(
		self,
		interaction: discord.Interaction,
		response: app_commands.Range[str, 1, 2000],
		trigger: app_commands.Range[str, 1, 500],
		chance: app_commands.Range[float, 0.0, 1.0] = 1.0,
		cooldown_seconds: app_commands.Range[int, 0, 31536000] = 0,
		regex: bool = False,
	):
		if not await require_manager(interaction):
			return
		try:
			validate_regex(trigger, regex)
		except ValueError as exc:
			await interaction.response.send_message(str(exc), ephemeral=True)
			return
		response_id = db.add_auto_response(interaction.guild_id, response, interaction.user.id)
		try:
			trigger_id = db.add_auto_response_trigger(
				interaction.guild_id,
				response_id,
				trigger,
				regex,
				chance,
				cooldown_seconds,
			)
		except ValueError as exc:
			db.remove_auto_response(interaction.guild_id, response_id)
			await interaction.response.send_message(str(exc), ephemeral=True)
			return
		await interaction.response.send_message(
			f"Created response `{response_id}` with trigger `{trigger_id}`.", ephemeral=True,
		)

	@app_commands.command(name="trigger-add", description="Add another trigger to a response.")
	@app_commands.describe(
		response_id="Response ID shown by /ab respond list.",
		trigger="Text or regular expression that activates the response.",
		chance="Chance to respond, from 0 through 1.",
		cooldown_seconds="Seconds before this trigger can respond again.",
		regex="Treat the trigger as a regular expression.",
	)
	async def trigger_add(
		self,
		interaction: discord.Interaction,
		response_id: app_commands.Range[int, 1],
		trigger: app_commands.Range[str, 1, 500],
		chance: app_commands.Range[float, 0.0, 1.0] = 1.0,
		cooldown_seconds: app_commands.Range[int, 0, 31536000] = 0,
		regex: bool = False,
	):
		if not await require_manager(interaction):
			return
		try:
			validate_regex(trigger, regex)
			trigger_id = db.add_auto_response_trigger(
				interaction.guild_id, response_id, trigger, regex, chance, cooldown_seconds,
			)
		except ValueError as exc:
			await interaction.response.send_message(str(exc), ephemeral=True)
			return
		await interaction.response.send_message(
			f"Added trigger `{trigger_id}` to response `{response_id}`.", ephemeral=True,
		)

	@app_commands.command(name="trigger-set", description="Change an existing response trigger.")
	@app_commands.describe(
		trigger_id="Trigger ID shown by /ab respond list.",
		trigger="Replacement trigger text or regular expression.",
		chance="Replacement response chance, from 0 through 1.",
		cooldown_seconds="Replacement cooldown in seconds.",
		regex="Whether the trigger is a regular expression.",
	)
	async def trigger_set(
		self,
		interaction: discord.Interaction,
		trigger_id: app_commands.Range[int, 1],
		trigger: app_commands.Range[str, 1, 500] | None = None,
		chance: app_commands.Range[float, 0.0, 1.0] | None = None,
		cooldown_seconds: app_commands.Range[int, 0, 31536000] | None = None,
		regex: bool | None = None,
	):
		if not await require_manager(interaction):
			return
		current = next(
			(
				item
				for response in db.get_auto_responses(interaction.guild_id)
				for item in response["triggers"]
				if item["trigger_id"] == trigger_id
			),
			None,
		)
		if not current:
			await interaction.response.send_message("That trigger was not found.", ephemeral=True)
			return
		if all(value is None for value in (trigger, chance, cooldown_seconds, regex)):
			await interaction.response.send_message("No trigger settings were changed.", ephemeral=True)
			return
		try:
			validate_regex(
				trigger if trigger is not None else current["trigger_text"],
				regex if regex is not None else current["is_regex"],
			)
			db.update_auto_response_trigger(
				interaction.guild_id,
				trigger_id,
				trigger_text=trigger,
				is_regex=regex,
				response_chance=chance,
				cooldown_seconds=cooldown_seconds,
			)
		except ValueError as exc:
			await interaction.response.send_message(str(exc), ephemeral=True)
			return
		await interaction.response.send_message(f"Updated trigger `{trigger_id}`.", ephemeral=True)

	@app_commands.command(name="trigger-remove", description="Remove a trigger from a response.")
	async def trigger_remove(
		self,
		interaction: discord.Interaction,
		trigger_id: app_commands.Range[int, 1],
	):
		if not await require_manager(interaction):
			return
		removed = db.remove_auto_response_trigger(interaction.guild_id, trigger_id)
		await interaction.response.send_message(
			f"Trigger `{trigger_id}` was {'removed' if removed else 'not found'}.", ephemeral=True,
		)

	@app_commands.command(name="sticker-create", description="Create a response with a sticker ID trigger.")
	@app_commands.describe(
		response="Message the bot should send.",
		sticker_id="Discord sticker ID copied with Developer Mode.",
		chance="Chance to respond, from 0 through 1.",
		cooldown_seconds="Seconds before this sticker trigger can respond again.",
	)
	async def sticker_create(
		self,
		interaction: discord.Interaction,
		response: app_commands.Range[str, 1, 2000],
		sticker_id: str,
		chance: app_commands.Range[float, 0.0, 1.0] = 1.0,
		cooldown_seconds: app_commands.Range[int, 0, 31536000] = 0,
	):
		if not await require_manager(interaction):
			return
		try:
			parsed_sticker_id = parse_sticker_id(sticker_id)
		except ValueError as exc:
			await interaction.response.send_message(str(exc), ephemeral=True)
			return
		response_id = db.add_auto_response(interaction.guild_id, response, interaction.user.id)
		try:
			sticker_trigger_id = db.add_auto_response_sticker_trigger(
				interaction.guild_id,
				response_id,
				parsed_sticker_id,
				chance,
				cooldown_seconds,
			)
		except ValueError as exc:
			db.remove_auto_response(interaction.guild_id, response_id)
			await interaction.response.send_message(str(exc), ephemeral=True)
			return
		await interaction.response.send_message(
			f"Created response `{response_id}` with sticker trigger "
			f"`{sticker_trigger_id}`.",
			ephemeral=True,
		)

	@app_commands.command(name="sticker-add", description="Add a sticker ID trigger to a response.")
	@app_commands.describe(
		response_id="Response ID shown by /ab respond list.",
		sticker_id="Discord sticker ID copied with Developer Mode.",
		chance="Chance to respond, from 0 through 1.",
		cooldown_seconds="Seconds before this sticker trigger can respond again.",
	)
	async def sticker_add(
		self,
		interaction: discord.Interaction,
		response_id: app_commands.Range[int, 1],
		sticker_id: str,
		chance: app_commands.Range[float, 0.0, 1.0] = 1.0,
		cooldown_seconds: app_commands.Range[int, 0, 31536000] = 0,
	):
		if not await require_manager(interaction):
			return
		try:
			parsed_sticker_id = parse_sticker_id(sticker_id)
			sticker_trigger_id = db.add_auto_response_sticker_trigger(
				interaction.guild_id,
				response_id,
				parsed_sticker_id,
				chance,
				cooldown_seconds,
			)
		except ValueError as exc:
			await interaction.response.send_message(str(exc), ephemeral=True)
			return
		await interaction.response.send_message(
			f"Added sticker trigger `{sticker_trigger_id}` to response `{response_id}`.",
			ephemeral=True,
		)

	@app_commands.command(name="sticker-set", description="Change an existing sticker trigger.")
	@app_commands.describe(
		sticker_trigger_id="Sticker trigger ID shown by /ab respond list.",
		sticker_id="Replacement Discord sticker ID.",
		chance="Replacement response chance, from 0 through 1.",
		cooldown_seconds="Replacement cooldown in seconds.",
	)
	async def sticker_set(
		self,
		interaction: discord.Interaction,
		sticker_trigger_id: app_commands.Range[int, 1],
		sticker_id: str | None = None,
		chance: app_commands.Range[float, 0.0, 1.0] | None = None,
		cooldown_seconds: app_commands.Range[int, 0, 31536000] | None = None,
	):
		if not await require_manager(interaction):
			return
		if all(value is None for value in (sticker_id, chance, cooldown_seconds)):
			await interaction.response.send_message(
				"No sticker trigger settings were changed.", ephemeral=True,
			)
			return
		try:
			parsed_sticker_id = parse_sticker_id(sticker_id) if sticker_id is not None else None
			updated = db.update_auto_response_sticker_trigger(
				interaction.guild_id,
				sticker_trigger_id,
				sticker_id=parsed_sticker_id,
				response_chance=chance,
				cooldown_seconds=cooldown_seconds,
			)
		except ValueError as exc:
			await interaction.response.send_message(str(exc), ephemeral=True)
			return
		await interaction.response.send_message(
			f"Sticker trigger `{sticker_trigger_id}` was "
			f"{'updated' if updated else 'not found'}.",
			ephemeral=True,
		)

	@app_commands.command(name="sticker-remove", description="Remove a sticker trigger.")
	async def sticker_remove(
		self,
		interaction: discord.Interaction,
		sticker_trigger_id: app_commands.Range[int, 1],
	):
		if not await require_manager(interaction):
			return
		removed = db.remove_auto_response_sticker_trigger(
			interaction.guild_id, sticker_trigger_id,
		)
		await interaction.response.send_message(
			f"Sticker trigger `{sticker_trigger_id}` was "
			f"{'removed' if removed else 'not found'}.",
			ephemeral=True,
		)

	@app_commands.command(name="response-set", description="Change the message sent by a response.")
	async def response_set(
		self,
		interaction: discord.Interaction,
		response_id: app_commands.Range[int, 1],
		response: app_commands.Range[str, 1, 2000],
	):
		if not await require_manager(interaction):
			return
		updated = db.update_auto_response(interaction.guild_id, response_id, response)
		await interaction.response.send_message(
			f"Response `{response_id}` was {'updated' if updated else 'not found'}.", ephemeral=True,
		)

	@app_commands.command(name="remove", description="Remove a response and all of its triggers.")
	async def respond_remove(
		self,
		interaction: discord.Interaction,
		response_id: app_commands.Range[int, 1],
	):
		if not await require_manager(interaction):
			return
		removed = db.remove_auto_response(interaction.guild_id, response_id)
		await interaction.response.send_message(
			f"Response `{response_id}` was {'removed' if removed else 'not found'}.", ephemeral=True,
		)

	@app_commands.command(name="list", description="List configured responses and triggers.")
	async def respond_list(self, interaction: discord.Interaction):
		if not await require_manager(interaction):
			return
		settings = db.get_respond_settings(interaction.guild_id)
		await send_json_chunks(
			interaction,
			f"Automatic responses ({'enabled' if settings['enabled'] else 'disabled'}):",
			json.dumps(settings["responses"], indent=2, ensure_ascii=True),
		)


async def setup(bot):
	await attach_feature_cog(bot, RespondCog(bot))
