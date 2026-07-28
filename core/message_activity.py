from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import discord

from core.database import db
from core.logger import log


class MessageActivityCache:
	def __init__(self):
		self._messages = defaultdict(lambda: defaultdict(deque))
		self._channel_messages = defaultdict(
			lambda: defaultdict(lambda: defaultdict(deque))
		)
		self._message_ids = defaultdict(dict)
		self._ocr_ready_guilds = set()
		self._territory_ready_guilds = set()
		self._messages_since_prune = defaultdict(int)

	def _prune(self, guild_id: int, user_id: int, lookback_days: int):
		cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
		timestamps = self._messages[guild_id][user_id]
		while timestamps and timestamps[0] < cutoff:
			timestamps.popleft()

	def count(self, guild_id: int, user_id: int, lookback_days: int) -> int | None:
		if guild_id not in self._ocr_ready_guilds:
			return None
		self._prune(guild_id, user_id, lookback_days)
		return len(self._messages[guild_id][user_id])

	def count_before(self, message: discord.Message, lookback_days: int) -> int | None:
		count = self.count(message.guild.id, message.author.id, lookback_days)
		if count is None:
			return None
		if message.id in self._message_ids[message.guild.id]:
			return max(0, count - 1)
		return count

	def is_territory_ready(self, guild_id: int) -> bool:
		return guild_id in self._territory_ready_guilds

	def channel_leaderboard(
		self,
		guild_id: int,
		channel_id: int,
		lookback_hours: int = 24,
		limit: int = 5,
	) -> list[tuple[int, int]] | None:
		if guild_id not in self._territory_ready_guilds:
			return None

		cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
		counts = []
		users = self._channel_messages[guild_id][channel_id]
		for user_id in list(users):
			timestamps = users[user_id]
			while timestamps and timestamps[0] < cutoff:
				timestamps.popleft()
			if timestamps:
				counts.append((user_id, len(timestamps)))
			else:
				del users[user_id]

		return sorted(counts, key=lambda item: (-item[1], item[0]))[:limit]

	def _record(self, message: discord.Message):
		guild_id = message.guild.id
		self._messages[guild_id][message.author.id].append(message.created_at)
		self._channel_messages[guild_id][message.channel.id][message.author.id].append(
			message.created_at
		)
		self._message_ids[guild_id][message.id] = message.created_at

	def record_live(self, message: discord.Message):
		if not message.guild or message.author.bot:
			return
		if message.id in self._message_ids[message.guild.id]:
			return
		self._record(message)
		self._messages_since_prune[message.guild.id] += 1
		if self._messages_since_prune[message.guild.id] >= 1000:
			settings = db.get_guild_settings(message.guild.id)
			lookback_days = settings["single_image_lookback_days"] if settings else 7
			self._prune_guild(message.guild.id, lookback_days)

	def _record_history(self, message: discord.Message):
		if message.author.bot or message.id in self._message_ids[message.guild.id]:
			return
		self._record(message)

	def _prune_guild(self, guild_id: int, lookback_days: int):
		cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
		for user_id in list(self._messages[guild_id]):
			self._prune(guild_id, user_id, lookback_days)
			if not self._messages[guild_id][user_id]:
				del self._messages[guild_id][user_id]
		for channel_id in list(self._channel_messages[guild_id]):
			users = self._channel_messages[guild_id][channel_id]
			for user_id in list(users):
				timestamps = users[user_id]
				while timestamps and timestamps[0] < cutoff:
					timestamps.popleft()
				if not timestamps:
					del users[user_id]
			if not users:
				del self._channel_messages[guild_id][channel_id]
		self._message_ids[guild_id] = {
			message_id: created_at
			for message_id, created_at in self._message_ids[guild_id].items()
			if created_at >= cutoff
		}
		self._messages_since_prune[guild_id] = 0

	async def _scan_history(self, channel, cutoff: datetime):
		try:
			async for message in channel.history(limit=None, after=cutoff, oldest_first=True):
				self._record_history(message)
		except (discord.Forbidden, discord.HTTPException, AttributeError) as exc:
			log.info(f"[ACTIVITY SKIP] channel={channel.id} error={exc}")

	def _sort_guild(self, guild_id: int):
		for user_id, timestamps in self._messages[guild_id].items():
			self._messages[guild_id][user_id] = deque(sorted(timestamps))
		for users in self._channel_messages[guild_id].values():
			for user_id, timestamps in users.items():
				users[user_id] = deque(sorted(timestamps))

	async def initialize_territory_channel(self, channel: discord.TextChannel):
		cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
		await self._scan_history(channel, cutoff)
		self._sort_guild(channel.guild.id)

	async def initialize_territory_guild(self, guild: discord.Guild):
		self._territory_ready_guilds.discard(guild.id)
		settings = db.get_territory_settings(guild.id)
		blacklist = settings["channel_blacklist"]
		cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
		channels = [
			channel for channel in guild.text_channels
			if channel.id not in blacklist
		]
		log.info(
			f"[TERRITORY ACTIVITY LOAD] guild={guild.id} channels={len(channels)} "
			f"blacklisted={len(blacklist)} lookback_hours=24"
		)
		for index, channel in enumerate(channels, start=1):
			await self._scan_history(channel, cutoff)
			if index % 10 == 0:
				log.info(
					f"[TERRITORY ACTIVITY PROGRESS] guild={guild.id} "
					f"channels={index}/{len(channels)}"
				)

		self._sort_guild(guild.id)
		self._territory_ready_guilds.add(guild.id)
		log.info(
			f"[TERRITORY ACTIVITY READY] guild={guild.id} channels={len(channels)} "
			"lookback_hours=24"
		)

	async def initialize_ocr_guild(self, guild: discord.Guild):
		self._ocr_ready_guilds.discard(guild.id)
		settings = db.get_ocr_settings(guild.id)
		lookback_days = max(settings["single_image_lookback_days"], 1) if settings else 7
		cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
		log.info(
			f"[OCR ACTIVITY LOAD] guild={guild.id} lookback_days={lookback_days}"
		)

		channels = [channel for channel in guild.text_channels]
		threads = {thread.id: thread for thread in guild.threads}
		for parent in guild.channels:
			if not isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
				continue
			try:
				async for thread in parent.archived_threads(limit=None, before=datetime.now(timezone.utc)):
					if thread.archive_timestamp and thread.archive_timestamp < cutoff:
						break
					threads[thread.id] = thread
			except (discord.Forbidden, discord.HTTPException, AttributeError) as exc:
				log.info(f"[ACTIVITY THREAD SKIP] channel={parent.id} error={exc}")
			if isinstance(parent, discord.TextChannel):
				try:
					async for thread in parent.archived_threads(private=True, joined=True, limit=None):
						if thread.archive_timestamp and thread.archive_timestamp < cutoff:
							break
						threads[thread.id] = thread
				except (discord.Forbidden, discord.HTTPException) as exc:
					log.info(f"[ACTIVITY PRIVATE THREAD SKIP] channel={parent.id} error={exc}")

		for channel in [*channels, *threads.values()]:
			await self._scan_history(channel, cutoff)

		self._sort_guild(guild.id)
		self._prune_guild(guild.id, lookback_days)
		self._ocr_ready_guilds.add(guild.id)
		log.info(
			f"[OCR ACTIVITY READY] guild={guild.id} users={len(self._messages[guild.id])} "
			f"lookback_days={lookback_days}"
		)

	async def initialize(self, bot):
		await bot.wait_until_ready()
		for guild in bot.guilds:
			if bot.get_cog("TerritoryCog"):
				try:
					await self.initialize_territory_guild(guild)
				except Exception as exc:
					log.info(f"[TERRITORY ACTIVITY ERROR] guild={guild.id} error={exc}")
			if bot.get_cog("OcrCog"):
				try:
					await self.initialize_ocr_guild(guild)
				except Exception as exc:
					log.info(f"[OCR ACTIVITY ERROR] guild={guild.id} error={exc}")


message_activity = MessageActivityCache()
