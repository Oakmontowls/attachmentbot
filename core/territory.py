import asyncio
import time

import discord

from core.database import db
from core.logger import log
from core.message_activity import message_activity


WINDOW_HOURS = 24
DEFAULT_UPDATE_INTERVAL_MINUTES = 60
UNCLAIMED_LINE = "Unclaimed territory"
MAX_TOPIC_LENGTH = 1024


def build_territory_topic(
	current_topic: str | None,
	new_line: str,
	previous_state: dict | None,
) -> tuple[str, bool]:
	topic = current_topic or ""
	first_line, separator, remaining = topic.partition("\n")

	if "territory" in first_line.casefold():
		line_created = previous_state["line_created"] if previous_state else False
		return new_line + (separator + remaining if separator else ""), line_created

	return new_line + (f"\n{topic}" if topic else ""), True


def remove_created_territory_line(
	current_topic: str | None,
	state: dict,
) -> tuple[str | None, bool]:
	if not state["line_created"]:
		return current_topic, False

	topic = current_topic or ""
	first_line, separator, remaining = topic.partition("\n")
	if first_line != state["last_managed_line"]:
		return current_topic, False

	return (remaining if separator and remaining else None), True


class TerritoryService:
	def __init__(self):
		self._next_updates = {}

	def leaderboard(
		self,
		guild_id: int,
		channel_id: int,
		limit: int = 5,
	) -> list[tuple[int, int]] | None:
		return message_activity.channel_leaderboard(
			guild_id,
			channel_id,
			lookback_hours=WINDOW_HOURS,
			limit=limit,
		)

	def update_interval_minutes(self, guild_id: int) -> int:
		settings = db.get_territory_settings(guild_id)
		return settings.get("update_interval_minutes", DEFAULT_UPDATE_INTERVAL_MINUTES)

	def set_update_interval(self, guild_id: int, minutes: int):
		db.set_territory_update_interval(guild_id, minutes)
		self._next_updates[guild_id] = 0

	def display_name(self, guild: discord.Guild, user_id: int) -> str:
		member = guild.get_member(user_id)
		name = member.display_name if member else f"User {user_id}"
		return name.replace("\r", " ").replace("\n", " ").strip()

	def territory_line(self, guild: discord.Guild, channel_id: int) -> str | None:
		leaders = self.leaderboard(guild.id, channel_id, limit=1)
		if leaders is None:
			return None
		if not leaders:
			return UNCLAIMED_LINE
		return f"{self.display_name(guild, leaders[0][0])}'s territory"

	async def update_channel(self, guild: discord.Guild, channel: discord.TextChannel):
		line = self.territory_line(guild, channel.id)
		if line is None:
			return

		state = db.get_territory_channel_state(guild.id, channel.id)
		new_topic, line_created = build_territory_topic(channel.topic, line, state)
		if len(new_topic) > MAX_TOPIC_LENGTH:
			log.info(
				f"[TERRITORY SKIP] guild={guild.id} channel={channel.id} topic would exceed "
				f"{MAX_TOPIC_LENGTH} characters"
			)
			return

		if new_topic != (channel.topic or ""):
			try:
				await channel.edit(topic=new_topic, reason="Territory leaderboard update")
			except discord.DiscordException as exc:
				log.info(f"[TERRITORY EDIT ERROR] guild={guild.id} channel={channel.id} error={exc}")
				return

		db.set_territory_channel_state(guild.id, channel.id, line, line_created)

	async def remove_channel_line(self, guild: discord.Guild, channel: discord.TextChannel):
		state = db.get_territory_channel_state(guild.id, channel.id)
		if not state:
			return
		try:
			fresh_channel = await guild.fetch_channel(channel.id)
			if isinstance(fresh_channel, discord.TextChannel):
				channel = fresh_channel
		except discord.DiscordException as exc:
			log.info(f"[TERRITORY FETCH ERROR] guild={guild.id} channel={channel.id} error={exc}")
			return

		new_topic, should_edit = remove_created_territory_line(channel.topic, state)
		if should_edit:
			try:
				await channel.edit(topic=new_topic, reason="Territory tracking disabled")
			except discord.DiscordException as exc:
				log.info(f"[TERRITORY REMOVE ERROR] guild={guild.id} channel={channel.id} error={exc}")
				return

		db.remove_territory_channel_state(guild.id, channel.id)

	async def update_guild(self, guild: discord.Guild):
		settings = db.get_territory_settings(guild.id)
		if not settings["enabled"] or not message_activity.is_territory_ready(guild.id):
			return

		blacklist = settings["channel_blacklist"]
		states = db.get_territory_channel_states(guild.id)
		for channel_id in blacklist & states.keys():
			channel = guild.get_channel(channel_id)
			if isinstance(channel, discord.TextChannel):
				await self.remove_channel_line(guild, channel)
			else:
				db.remove_territory_channel_state(guild.id, channel_id)
		for channel in guild.text_channels:
			if channel.id not in blacklist:
				await self.update_channel(guild, channel)

	async def force_update(self, guild: discord.Guild) -> str:
		settings = db.get_territory_settings(guild.id)
		if not settings["enabled"]:
			return "disabled"
		if not message_activity.is_territory_ready(guild.id):
			return "loading"

		await self.update_guild(guild)
		self._next_updates[guild.id] = (
			time.monotonic() + settings["update_interval_minutes"] * 60
		)
		return "updated"

	async def remove_guild_lines(self, guild: discord.Guild):
		states = db.get_territory_channel_states(guild.id)
		for channel_id in states:
			channel = guild.get_channel(channel_id)
			if isinstance(channel, discord.TextChannel):
				await self.remove_channel_line(guild, channel)
			else:
				db.remove_territory_channel_state(guild.id, channel_id)

	async def set_enabled(self, guild: discord.Guild, enabled: bool):
		db.set_territory_enabled(guild.id, enabled)
		self._next_updates[guild.id] = 0
		if enabled:
			await self.update_guild(guild)
		else:
			await self.remove_guild_lines(guild)

	async def set_channel_blacklisted(
		self,
		guild: discord.Guild,
		channel: discord.TextChannel,
		blacklisted: bool,
	):
		if blacklisted:
			db.add_territory_blacklist(guild.id, channel.id)
			await self.remove_channel_line(guild, channel)
			return

		db.remove_territory_blacklist(guild.id, channel.id)
		await message_activity.initialize_territory_channel(channel)
		if db.get_territory_settings(guild.id)["enabled"]:
			await self.update_channel(guild, channel)

	async def start(self, bot):
		await bot.wait_until_ready()
		while not bot.is_closed():
			now = time.monotonic()
			for guild in bot.guilds:
				try:
					settings = db.get_territory_settings(guild.id)
					if not settings["enabled"]:
						if db.get_territory_channel_states(guild.id):
							await self.remove_guild_lines(guild)
						self._next_updates.pop(guild.id, None)
						continue
					if not message_activity.is_territory_ready(guild.id):
						continue
					if now < self._next_updates.get(guild.id, 0):
						continue
					await self.update_guild(guild)
					self._next_updates[guild.id] = (
						now + settings["update_interval_minutes"] * 60
					)
				except Exception as exc:
					log.info(f"[TERRITORY ERROR] guild={guild.id} error={exc}")
			await asyncio.sleep(30)


territory_service = TerritoryService()
