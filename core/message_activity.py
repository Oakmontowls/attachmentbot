from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import discord

from core.database import db
from core.logger import log


class MessageActivityCache:
	def __init__(self):
		self._messages = defaultdict(lambda: defaultdict(deque))
		self._message_ids = defaultdict(dict)
		self._ready_guilds = set()
		self._messages_since_prune = defaultdict(int)

	def _prune(self, guild_id: int, user_id: int, lookback_days: int):
		cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
		timestamps = self._messages[guild_id][user_id]
		while timestamps and timestamps[0] < cutoff:
			timestamps.popleft()

	def count(self, guild_id: int, user_id: int, lookback_days: int) -> int | None:
		if guild_id not in self._ready_guilds:
			return None
		self._prune(guild_id, user_id, lookback_days)
		return len(self._messages[guild_id][user_id])

	def record_live(self, message: discord.Message):
		if not message.guild or message.author.bot:
			return
		if message.id in self._message_ids[message.guild.id]:
			return
		self._messages[message.guild.id][message.author.id].append(message.created_at)
		self._message_ids[message.guild.id][message.id] = message.created_at
		self._messages_since_prune[message.guild.id] += 1
		if self._messages_since_prune[message.guild.id] >= 1000:
			settings = db.get_guild_settings(message.guild.id)
			lookback_days = settings["single_image_lookback_days"] if settings else 7
			self._prune_guild(message.guild.id, lookback_days)

	def _record_history(self, message: discord.Message):
		if message.author.bot or message.id in self._message_ids[message.guild.id]:
			return
		self._messages[message.guild.id][message.author.id].append(message.created_at)
		self._message_ids[message.guild.id][message.id] = message.created_at

	def _prune_guild(self, guild_id: int, lookback_days: int):
		cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
		for user_id in list(self._messages[guild_id]):
			self._prune(guild_id, user_id, lookback_days)
			if not self._messages[guild_id][user_id]:
				del self._messages[guild_id][user_id]
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

	async def initialize_guild(self, guild: discord.Guild):
		self._ready_guilds.discard(guild.id)
		settings = db.get_guild_settings(guild.id)
		lookback_days = settings["single_image_lookback_days"] if settings else 7
		cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

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

		for user_id, timestamps in self._messages[guild.id].items():
			self._messages[guild.id][user_id] = deque(sorted(timestamps))
		self._prune_guild(guild.id, lookback_days)
		self._ready_guilds.add(guild.id)
		log.info(
			f"[ACTIVITY READY] guild={guild.id} users={len(self._messages[guild.id])} "
			f"lookback_days={lookback_days}"
		)

	async def initialize(self, bot):
		await bot.wait_until_ready()
		for guild in bot.guilds:
			try:
				await self.initialize_guild(guild)
			except Exception as exc:
				log.info(f"[ACTIVITY ERROR] guild={guild.id} error={exc}")


message_activity = MessageActivityCache()
