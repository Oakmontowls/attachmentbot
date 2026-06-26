import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

import discord

from core.database import db
from core.logger import log
from core.temporary_roles import assign_temporary_role

URL_PATTERN = re.compile(r"https?://|discord\.gg/|www\.", re.IGNORECASE)
CUSTOM_EMOTE_PATTERN = re.compile(r"^<a?:[A-Za-z0-9_]{2,32}:\d{15,25}>$")
GIF_URL_PATTERN = re.compile(r"\.gif(?:\?|$)", re.IGNORECASE)
OWO_PATTERN = re.compile(r"^owo$", re.IGNORECASE)


@dataclass
class PressureState:
	pressure: float = 0.0
	last_seen: float = 0.0
	last_content: str = ""


class PressureModerator:
	def __init__(self):
		self.user_states: dict[tuple[int, int, int], PressureState] = {}

	def reset_user(self, guild_id: int, user_id: int):
		for key in list(self.user_states):
			if key[0] == guild_id and key[2] == user_id:
				self.user_states.pop(key, None)

	def reset_guild(self, guild_id: int):
		for key in list(self.user_states):
			if key[0] == guild_id:
				self.user_states.pop(key, None)

	def current_pressure(self, guild_id: int, channel_id: int, user_id: int) -> int:
		state = self.user_states.get((guild_id, channel_id, user_id))
		if not state:
			return 0

		settings = db.get_pressure_settings(guild_id)
		return round(self.decayed_pressure(state, settings, time.monotonic()))

	def get_state(self, guild_id: int, channel_id: int, user_id: int) -> PressureState | None:
		return self.user_states.get((guild_id, channel_id, user_id))

	def current_channel_pressures(self, guild_id: int, user_id: int) -> list[tuple[int, int, int]]:
		settings = db.get_pressure_settings(guild_id)
		now = time.monotonic()
		pressures = []

		for key, state in self.user_states.items():
			state_guild_id, channel_id, state_user_id = key
			if state_guild_id != guild_id or state_user_id != user_id:
				continue

			current = round(self.decayed_pressure(state, settings, now))
			if current <= 0:
				continue

			threshold = self.threshold_for_channel(guild_id, channel_id, settings)
			pressures.append((channel_id, current, threshold))

		return sorted(pressures, key=lambda row: row[1], reverse=True)

	def current_channel_pressures_for_guild(
		self,
		guild: discord.Guild,
		user_id: int,
	) -> list[tuple[int, int, int]]:
		settings = db.get_pressure_settings(guild.id)
		now = time.monotonic()
		pressures = []

		for key, state in self.user_states.items():
			state_guild_id, channel_id, state_user_id = key
			if state_guild_id != guild.id or state_user_id != user_id:
				continue

			current = round(self.decayed_pressure(state, settings, now))
			if current <= 0:
				continue

			channel = guild.get_channel_or_thread(channel_id)
			threshold = self.threshold_for_channel_object(guild.id, channel_id, channel, settings)
			pressures.append((channel_id, current, threshold))

		return sorted(pressures, key=lambda row: row[1], reverse=True)

	def decayed_pressure(self, state: PressureState, settings: dict, now: float) -> float:
		if not state.last_seen:
			return 0.0

		elapsed = max(0.0, now - state.last_seen)
		return max(0.0, state.pressure - (elapsed * settings["decay_per_second"]))

	def threshold_for_channel(self, guild_id: int, channel_id: int, settings: dict) -> int:
		override = db.get_pressure_channel_threshold(guild_id, channel_id)
		if override is not None:
			return override
		return settings["threshold"]

	def threshold_for_channel_object(
		self,
		guild_id: int,
		channel_id: int,
		channel,
		settings: dict,
	) -> int:
		threshold = self.threshold_for_channel(guild_id, channel_id, settings)
		parent_id = getattr(channel, "parent_id", None) if channel else None
		if parent_id is None:
			return threshold

		parent_override = db.get_pressure_channel_threshold(guild_id, parent_id)
		return parent_override if parent_override is not None else threshold

	def threshold_for_message(self, message: discord.Message, settings: dict) -> int:
		return self.threshold_for_channel_object(
			message.guild.id,
			message.channel.id,
			message.channel,
			settings,
		)

	def normalize_content(self, content: str) -> str:
		return " ".join(content.lower().split())

	def is_solo_emote(self, content: str) -> bool:
		content = content.strip()
		if not content or OWO_PATTERN.fullmatch(content):
			return False

		if CUSTOM_EMOTE_PATTERN.fullmatch(content):
			return True

		cleaned = content.replace("\ufe0f", "").replace("\u200d", "")
		if not cleaned:
			return False

		for char in cleaned:
			if char.isspace():
				continue
			category = unicodedata.category(char)
			name = unicodedata.name(char, "")
			if category == "So" or "EMOJI" in name:
				continue
			return False

		return True

	def is_gif_attachment(self, attachment: discord.Attachment) -> bool:
		content_type = attachment.content_type or ""
		filename = attachment.filename or ""
		url = attachment.url or ""
		return (
			content_type.lower() == "image/gif"
			or filename.lower().endswith(".gif")
			or GIF_URL_PATTERN.search(url) is not None
		)

	def banned_word_hits(self, content: str, banned_words: list[str]) -> list[str]:
		if not banned_words:
			return []

		text = content.lower()
		pattern = "(" + "|".join(banned_words) + ")"
		try:
			return [match.group(0) for match in re.finditer(pattern, text)]
		except re.error as exc:
			log.info(f"[PRESSURE REGEX ERROR] {exc}")
			return []

	def new_member_multiplier(self, message: discord.Message, settings: dict) -> float:
		if not isinstance(message.author, discord.Member) or not message.author.joined_at:
			return 0.0

		joined_at = message.author.joined_at
		if joined_at.tzinfo is None:
			joined_at = joined_at.replace(tzinfo=timezone.utc)

		member_age_hours = (datetime.now(timezone.utc) - joined_at).total_seconds() / 3600
		if member_age_hours <= settings["new_member_hours"]:
			return settings["new_member_pressure"]

		return 0.0

	def calculate_increment(self, message: discord.Message, settings: dict, state: PressureState) -> tuple[int, list[str]]:
		increment = settings["base_pressure"]
		reasons = [f"message +{settings['base_pressure']}"]

		gif_attachments = [attachment for attachment in message.attachments if self.is_gif_attachment(attachment)]
		image_attachments = [
			attachment for attachment in message.attachments
			if attachment.content_type
			and attachment.content_type.startswith("image/")
			and attachment not in gif_attachments
		]

		if image_attachments:
			value = len(image_attachments) * settings["attachment_pressure"]
			increment += value
			reasons.append(f"images +{value}")

		gif_count = len(gif_attachments)
		if GIF_URL_PATTERN.search(message.content or ""):
			gif_count += 1
		if gif_count:
			value = gif_count * settings["gif_pressure"]
			increment += value
			reasons.append(f"gifs +{value}")

		if message.embeds:
			value = len(message.embeds) * settings["embed_pressure"]
			increment += value
			reasons.append(f"embeds +{value}")

		mentions = len(message.mentions) + len(message.role_mentions)
		if mentions:
			value = mentions * settings["mention_pressure"]
			increment += value
			reasons.append(f"mentions +{value}")

		line_count = (message.content or "").count("\n")
		if line_count:
			value = line_count * settings["line_pressure"]
			increment += value
			reasons.append(f"newlines +{value}")

		if self.is_solo_emote(message.content or ""):
			value = settings["solo_emote_pressure"]
			increment += value
			reasons.append(f"solo emote +{value}")

		banned_hits = self.banned_word_hits(message.content or "", settings.get("banned_words", []))
		if banned_hits:
			value = len(banned_hits) * settings["banned_word_pressure"]
			increment += value
			reasons.append(f"banned words +{value}: {', '.join(banned_hits[:5])}")

		if URL_PATTERN.search(message.content or ""):
			value = settings["link_pressure"]
			increment += value
			reasons.append(f"link +{value}")

		normalized = self.normalize_content(message.content or "")
		if normalized and normalized == state.last_content:
			value = settings["duplicate_pressure"]
			increment += value
			reasons.append(f"duplicate +{value}")

		new_member_value = self.new_member_multiplier(message, settings)
		if new_member_value:
			increment += new_member_value
			reasons.append(f"new member +{new_member_value}")

		state.last_content = normalized
		return increment, reasons

	async def process_message(self, bot, message: discord.Message, config: dict) -> bool:
		settings = config.get("pressure") or db.get_pressure_settings(message.guild.id)
		if not settings["enabled"]:
			return False

		now = time.monotonic()
		key = (message.guild.id, message.channel.id, message.author.id)
		state = self.user_states.setdefault(key, PressureState(last_seen=now))

		state.pressure = self.decayed_pressure(state, settings, now)
		threshold = self.threshold_for_message(message, settings)
		already_over_threshold = state.pressure >= threshold
		increment, reasons = self.calculate_increment(message, settings, state)
		state.pressure += increment
		state.last_seen = now

		if state.pressure < threshold:
			return False

		log.info(
			f"[PRESSURE THRESHOLD] guild={message.guild.id} user={message.author.id} "
			f"pressure={round(state.pressure)}/{threshold} channel={message.channel.id} reasons={', '.join(reasons)}"
		)

		if settings["delete_message"]:
			await self.delete_threshold_message(message)
		if already_over_threshold:
			reasons.insert(0, "already over threshold")

		await self.take_action(bot, message, settings, threshold, state.pressure, reasons, config)

		return True

	async def delete_threshold_message(self, message: discord.Message):
		try:
			await message.delete()
		except discord.NotFound:
			pass
		except discord.DiscordException as exc:
			log.info(f"[PRESSURE DELETE ERROR] message={message.id} error={exc}")

	async def take_action(
		self,
		bot,
		message: discord.Message,
		settings: dict,
		threshold: int,
		pressure: float,
		reasons: list[str],
		config: dict,
	):
		role = message.guild.get_role(config["role_id"]) if config.get("role_id") else None
		log_channel_id = settings.get("log_channel_id") or config.get("log_channel_id")
		log_channel = bot.get_channel(log_channel_id) if log_channel_id else None

		if settings["give_role"]:
			if role:
				try:
					await assign_temporary_role(
						message.author,
						role,
						settings["role_duration_seconds"],
						"Pressure moderation threshold reached",
					)
				except discord.DiscordException as exc:
					log.info(f"[PRESSURE ROLE ERROR] message={message.id} error={exc}")
			else:
				log.info("[PRESSURE ROLE SKIP] No role set")

		if log_channel:
			embed = discord.Embed(
				title="Pressure Moderation Triggered",
				color=discord.Color.orange(),
				description=(
					f"User: {message.author.mention}\n"
					f"Channel: {message.channel.mention}\n"
					f"Message ID: {message.id}\n"
					f"Pressure: {round(pressure)}/{threshold}"
				),
			)
			embed.add_field(name="Reasons", value=", ".join(reasons)[:1024], inline=False)
			if message.content:
				embed.add_field(name="Message", value=message.content[:1024], inline=False)
			await log_channel.send(embed=embed)


pressure_moderator = PressureModerator()
