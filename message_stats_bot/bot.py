import asyncio
import hashlib
import logging
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import commands

from message_stats_bot.database import StatsDatabase


logging.basicConfig(
	level=logging.INFO,
	format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("message-stats")

AVATAR_USER_ID = re.compile(r"/avatars/(\d+)/")
MIGRATED_TIMESTAMP = re.compile(
	r"(?P<date>\d{1,2}/\d{1,2}/\d{4})[^\r\n\d]{0,16}"
	r"(?P<time>\d{1,2}:\d{2}\s*[AP]M)",
	re.IGNORECASE,
)


def positive_int_env(name: str, default: int) -> int:
	try:
		return max(1, int(os.getenv(name, str(default))))
	except ValueError:
		log.warning("Invalid %s; using %s", name, default)
		return default


def configure_event_loop():
	"""Avoid noisy Proactor socket-shutdown errors on Windows Python 3.11."""
	if sys.platform == "win32":
		asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def timestamp(value: datetime) -> int:
	return int(value.timestamp())


def migration_timezone() -> ZoneInfo:
	name = os.getenv("MESSAGE_STATS_MIGRATION_TIMEZONE", "America/Los_Angeles")
	try:
		return ZoneInfo(name)
	except ZoneInfoNotFoundError:
		log.warning("Unknown migration timezone %s; using UTC", name)
		return ZoneInfo("UTC")


def migrated_message_id(message_id: int, embed_index: int) -> int:
	"""Create a stable negative SQLite ID for one embed in a bot message."""
	digest = hashlib.blake2b(
		f"{message_id}:{embed_index}".encode("ascii"),
		digest_size=8,
		person=b"msgstats",
	).digest()
	value = int.from_bytes(digest, "big") & ((1 << 63) - 1)
	return -(value or 1)


class HistoryScanInterrupted(Exception):
	def __init__(
		self,
		cause: discord.HTTPException,
		inserted: int,
		*,
		retryable: bool,
	):
		super().__init__(str(cause))
		self.cause = cause
		self.inserted = inserted
		self.retryable = retryable


def migrated_embed_author(
	embed: discord.Embed,
	zone: ZoneInfo,
) -> tuple[int, str, str, int | None] | None:
	"""Extract original identity and time from one Tilarta-style embed."""
	payload = embed.to_dict()
	author = payload.get("author") or {}
	icon_url = str(author.get("icon_url") or author.get("proxy_icon_url") or "")
	id_match = AVATAR_USER_ID.search(icon_url)
	name = str(author.get("name") or "").strip()
	if not id_match or not name:
		return None

	text_parts = [str(payload.get("description") or "")]
	for field in payload.get("fields") or []:
		text_parts.extend((str(field.get("name") or ""), str(field.get("value") or "")))
	date_match = MIGRATED_TIMESTAMP.search("\n".join(text_parts))
	if date_match:
		original = datetime.strptime(
			f"{date_match.group('date')} {date_match.group('time').upper()}",
			"%m/%d/%Y %I:%M %p",
		).replace(tzinfo=zone)
	elif embed.timestamp is not None:
		original = embed.timestamp
	else:
		original = None

	return (
		int(id_match.group(1)),
		name,
		icon_url,
		timestamp(original) if original is not None else None,
	)


def migrated_embed_records(
	message_id: int,
	embeds: list[discord.Embed],
	zone: ZoneInfo,
	prior: tuple[int, int] | None = None,
) -> tuple[list[tuple[int, int, str, str, int]], tuple[int, int] | None]:
	"""Resolve migrated embeds, inheriting time for consecutive same-author posts."""
	records = []
	state = prior
	for index, embed in enumerate(embeds):
		parsed = migrated_embed_author(embed, zone)
		if parsed is None:
			continue
		user_id, name, avatar_url, created_at = parsed
		if created_at is None:
			if state is None or state[0] != user_id:
				continue
			created_at = state[1]
		else:
			state = (user_id, created_at)
		records.append(
			(migrated_message_id(message_id, index), user_id, name, avatar_url, created_at)
		)
	return records, state


def channel_created_at(channel: discord.abc.GuildChannel | discord.Thread) -> datetime:
	"""Return a creation time even for partial thread objects from Discord."""
	created_at = getattr(channel, "created_at", None)
	if created_at is not None:
		return created_at
	return discord.utils.snowflake_time(channel.id)


def channel_kind(channel: discord.abc.GuildChannel | discord.Thread) -> str:
	if isinstance(channel, discord.Thread):
		return "forum_post" if isinstance(channel.parent, discord.ForumChannel) else "thread"
	if isinstance(channel, discord.ForumChannel):
		return "forum"
	if isinstance(channel, discord.VoiceChannel):
		return "voice_text"
	if isinstance(channel, discord.TextChannel) and channel.is_news():
		return "announcement"
	return "text"


class MessageCollector(commands.Cog):
	def __init__(self, bot: "MessageStatsBot"):
		self.bot = bot
		self.stored_channel_ids: set[int] = set()
		self.migration_context: dict[int, tuple[int, int]] = {}
		self.migration_diagnostics: Counter[str] = Counter()
		self.migration_source_ids: set[int] = set()

	async def store_channel(
		self,
		channel: discord.abc.GuildChannel | discord.Thread,
		*,
		force: bool = False,
	):
		if not isinstance(
			channel,
			(discord.TextChannel, discord.VoiceChannel, discord.ForumChannel, discord.Thread),
		):
			return
		if channel.id in self.stored_channel_ids and not force:
			return
		await self.bot.db_call(
			self.bot.stats.upsert_channel,
			guild_id=channel.guild.id,
			channel_id=channel.id,
			parent_channel_id=channel.parent_id if isinstance(channel, discord.Thread) else None,
			name=channel.name,
			kind=channel_kind(channel),
			owner_id=channel.owner_id if isinstance(channel, discord.Thread) else None,
			created_at=timestamp(channel_created_at(channel)),
			archived=channel.archived if isinstance(channel, discord.Thread) else False,
		)
		self.stored_channel_ids.add(channel.id)

	def message_records(
		self,
		message: discord.Message,
	) -> list[
		tuple[
			tuple[int, int, int, int | None, int, int],
			tuple[int, int, str, str, str | None, int],
		]
	]:
		if not message.guild:
			return []
		if not isinstance(
			message.channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread)
		):
			return []

		parent_id = message.channel.parent_id if isinstance(message.channel, discord.Thread) else None
		now = int(datetime.now(timezone.utc).timestamp())
		if not message.author.bot:
			row = (
				message.id,
				message.guild.id,
				message.channel.id,
				parent_id,
				message.author.id,
				timestamp(message.created_at),
			)
			member = (
				message.guild.id,
				message.author.id,
				str(message.author),
				getattr(message.author, "display_name", message.author.name),
				str(message.author.display_avatar.url),
				now,
			)
			return [(row, member)]

		if not (
			isinstance(message.channel, discord.Thread)
			and isinstance(message.channel.parent, discord.ForumChannel)
			and message.channel.parent_id == self.bot.migration_forum_id
		):
			return []
		self.migration_diagnostics["bot_messages_in_target_forum"] += 1
		source_ids = {
			value
			for value in (
				message.author.id,
				getattr(message, "application_id", None),
				getattr(message, "webhook_id", None),
			)
			if value is not None
		}
		self.migration_source_ids.update(source_ids)
		if self.bot.migration_bot_user_id not in source_ids:
			self.migration_diagnostics["source_id_mismatch"] += 1
		else:
			self.migration_diagnostics["source_id_matched"] += 1
		self.migration_diagnostics["embeds_seen"] += len(message.embeds)

		migrated, state = migrated_embed_records(
			message.id,
			message.embeds,
			self.bot.migration_zone,
			self.migration_context.get(message.channel.id),
		)
		if state is not None:
			self.migration_context[message.channel.id] = state
		self.migration_diagnostics["embeds_reattributed"] += len(migrated)
		records = []
		for synthetic_id, user_id, name, avatar_url, created_at in migrated:
			row = (
				synthetic_id,
				message.guild.id,
				message.channel.id,
				parent_id,
				user_id,
				created_at,
			)
			member = (message.guild.id, user_id, name, name, avatar_url, now)
			records.append((row, member))
		return records

	async def store_message(self, message: discord.Message) -> bool:
		records = self.message_records(message)
		if not records:
			return False
		await self.store_channel(message.channel)
		return await self.bot.db_call(
			self.bot.stats.record_messages,
			[row for row, _member in records],
			[member for _row, member in records],
		) > 0

	async def scan_channel(
		self, channel: discord.TextChannel | discord.VoiceChannel | discord.Thread
	) -> int:
		await self.store_channel(channel)
		cursor, migration_user_id, migration_timestamp = await self.bot.db_call(
			self.bot.stats.history_state, channel.guild.id, channel.id
		)
		await self.bot.db_call(
			self.bot.stats.begin_history_scan, channel.guild.id, channel.id
		)
		if migration_user_id is not None and migration_timestamp is not None:
			self.migration_context[channel.id] = (migration_user_id, migration_timestamp)
		after = discord.Object(id=cursor) if cursor is not None else None
		if channel_kind(channel) in {"announcement", "text", "voice_text"}:
			log.info(
				"History scan start guild=%s channel=%s:%s resume_after=%s",
				channel.guild.id,
				channel_kind(channel),
				channel.name,
				cursor,
			)
		inserted = 0
		scanned = 0
		last_seen_id = cursor
		batch: list[tuple[int, int, int, int | None, int, int]] = []
		members: dict[int, tuple[int, int, str, str, str | None, int]] = {}
		try:
			async for message in channel.history(
				limit=None,
				after=after,
				oldest_first=True,
			):
				scanned += 1
				last_seen_id = message.id
				for row, member in self.message_records(message):
					batch.append(row)
					members[member[1]] = member
				if scanned % self.bot.batch_size == 0:
					inserted += await self.bot.db_call(
						self.bot.stats.record_messages, batch, list(members.values())
					)
					migration_state = self.migration_context.get(channel.id)
					await self.bot.db_call(
						self.bot.stats.update_history_cursor,
						channel.guild.id,
						channel.id,
						last_seen_id,
						migration_user_id=migration_state[0] if migration_state else None,
						migration_timestamp=migration_state[1] if migration_state else None,
					)
					batch.clear()
					members.clear()
					if scanned % (self.bot.batch_size * 10) == 0:
						log.info(
							"History scan progress guild=%s channel=%s:%s "
							"scanned=%s inserted=%s cursor=%s",
							channel.guild.id,
							channel_kind(channel),
							channel.name,
							scanned,
							inserted,
							last_seen_id,
						)
			inserted += await self.bot.db_call(
				self.bot.stats.record_messages, batch, list(members.values())
			)
			migration_state = self.migration_context.get(channel.id)
			await self.bot.db_call(
				self.bot.stats.update_history_cursor,
				channel.guild.id,
				channel.id,
				last_seen_id,
				complete=True,
				migration_user_id=migration_state[0] if migration_state else None,
				migration_timestamp=migration_state[1] if migration_state else None,
			)
		except (discord.Forbidden, discord.HTTPException) as exc:
			inserted += await self.bot.db_call(
				self.bot.stats.record_messages, batch, list(members.values())
			)
			if last_seen_id is not None:
				migration_state = self.migration_context.get(channel.id)
				await self.bot.db_call(
					self.bot.stats.update_history_cursor,
					channel.guild.id,
					channel.id,
					last_seen_id,
					migration_user_id=migration_state[0] if migration_state else None,
					migration_timestamp=migration_state[1] if migration_state else None,
				)
			raise HistoryScanInterrupted(
				exc,
				inserted,
				retryable=not isinstance(exc, discord.Forbidden),
			) from exc
		if channel_kind(channel) in {"announcement", "text", "voice_text"}:
			log.info(
				"History scan complete guild=%s channel=%s:%s scanned=%s inserted=%s",
				channel.guild.id,
				channel_kind(channel),
				channel.name,
				scanned,
				inserted,
			)
		return inserted

	async def archived_threads(
		self,
		parent: discord.TextChannel | discord.ForumChannel,
	):
		try:
			async for thread in parent.archived_threads(
				limit=None,
				before=datetime.now(timezone.utc),
			):
				yield thread
		except (discord.Forbidden, discord.HTTPException) as exc:
			log.warning("Could not list archived threads for %s: %s", parent.id, exc)

		if not isinstance(parent, discord.TextChannel):
			return
		try:
			async for thread in parent.archived_threads(
				private=True,
				joined=True,
				limit=None,
			):
				yield thread
		except (discord.Forbidden, discord.HTTPException) as exc:
			log.warning("Could not list private archived threads for %s: %s", parent.id, exc)

	async def import_guild_history(
		self, guild: discord.Guild
	) -> tuple[int, int, int, int, int]:
		self.migration_diagnostics.clear()
		self.migration_source_ids.clear()
		await self.bot.db_call(self.bot.stats.upsert_guild, guild.id, guild.name)
		locations: dict[
			int, discord.TextChannel | discord.VoiceChannel | discord.Thread
		] = {
			channel.id: channel for channel in guild.text_channels
		}
		locations.update({channel.id: channel for channel in guild.voice_channels})
		locations.update({thread.id: thread for thread in guild.threads})

		for parent in guild.channels:
			if not isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
				continue
			await self.store_channel(parent)
			async for thread in self.archived_threads(parent):
				locations[thread.id] = thread

		import_statuses = await self.bot.db_call(
			self.bot.stats.history_import_statuses, guild.id
		)

		def import_priority(channel):
			kind = channel_kind(channel)
			cursor, completed_at = import_statuses.get(channel.id, (None, None))
			# Resume interrupted, partially traversed channels before doing cheap
			# incremental checks of locations which already reached Discord's end.
			if cursor is not None and completed_at is None:
				return 0, channel.id
			if kind == "forum_post" and channel.parent_id == self.bot.migration_forum_id:
				return 1, channel.id
			if kind in {"announcement", "text", "voice_text"}:
				return 2, timestamp(channel_created_at(channel)), channel.id
			if kind == "forum_post":
				return 3, channel.id
			if kind == "thread":
				return 4, channel.id
			return 5, channel.id

		ordered_locations = sorted(
			locations.values(),
			key=import_priority,
		)

		async def scan_location(channel):
			async with semaphore:
				inserted_here = 0
				for attempt in range(1, self.bot.history_retry_attempts + 1):
					try:
						inserted_here += await self.scan_channel(channel)
						return channel, inserted_here, "complete"
					except asyncio.CancelledError:
						raise
					except HistoryScanInterrupted as exc:
						inserted_here += exc.inserted
						if not exc.retryable:
							log.warning(
								"History scan inaccessible guild=%s channel=%s name=%r: %s",
								guild.id,
								channel.id,
								channel.name,
								exc.cause,
							)
							return channel, inserted_here, "inaccessible"
						if attempt >= self.bot.history_retry_attempts:
							log.error(
								"History scan still incomplete after %s attempts guild=%s "
								"channel=%s name=%r: %s",
								attempt,
								guild.id,
								channel.id,
								channel.name,
								exc.cause,
							)
							return channel, inserted_here, "failed"
						delay = min(2 ** (attempt - 1), 30)
						log.warning(
							"History scan interrupted; resuming in %ss attempt=%s/%s "
							"guild=%s channel=%s name=%r: %s",
							delay,
							attempt + 1,
							self.bot.history_retry_attempts,
							guild.id,
							channel.id,
							channel.name,
							exc.cause,
						)
						await asyncio.sleep(delay)
					except Exception:
						log.exception(
							"History import failed location guild=%s channel=%s name=%r; continuing",
							guild.id,
							getattr(channel, "id", None),
							getattr(channel, "name", None),
						)
						return channel, inserted_here, "failed"

		semaphore = asyncio.Semaphore(self.bot.import_concurrency)
		tasks = [asyncio.create_task(scan_location(channel)) for channel in ordered_locations]
		inserted = 0
		failed = 0
		inaccessible = 0
		succeeded = 0
		try:
			completed = asyncio.as_completed(tasks)
			for index, result in enumerate(completed, start=1):
				channel, new_rows, status = await result
				inserted += new_rows
				failed += int(status == "failed")
				inaccessible += int(status == "inaccessible")
				succeeded += int(status == "complete")
				if index % 25 == 0 or index == len(locations):
					log.info(
						"History import guild=%s locations=%s/%s current=%s:%s inserted=%s "
						"status=%s complete=%s inaccessible=%s failed=%s "
						"migrated=%s migration_sources=%s",
						guild.id,
						index,
						len(locations),
						channel_kind(channel),
						channel.name,
						inserted,
						status,
						succeeded,
						inaccessible,
						failed,
						self.migration_diagnostics["embeds_reattributed"],
						sorted(self.migration_source_ids),
					)
		finally:
			for task in tasks:
				if not task.done():
					task.cancel()
			await asyncio.gather(*tasks, return_exceptions=True)
		if self.migration_diagnostics:
			log.info(
				"Migration diagnostics guild=%s forum=%s configured_source=%s "
				"observed_source_ids=%s counts=%s",
				guild.id,
				self.bot.migration_forum_id,
				self.bot.migration_bot_user_id,
				sorted(self.migration_source_ids),
				dict(self.migration_diagnostics),
			)
		return len(locations), inserted, succeeded, inaccessible, failed

	@commands.Cog.listener()
	async def on_message(self, message: discord.Message):
		await self.store_message(message)

	@commands.Cog.listener()
	async def on_thread_create(self, thread: discord.Thread):
		await self.store_channel(thread)

	@commands.Cog.listener()
	async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
		await self.store_channel(after, force=True)


class MessageStatsBot(commands.Bot):
	def __init__(self):
		intents = discord.Intents.none()
		intents.guilds = True
		intents.guild_messages = True
		intents.message_content = True
		super().__init__(
			command_prefix=commands.when_mentioned,
			help_command=None,
			intents=intents,
		)
		database_path = os.getenv(
			"MESSAGE_STATS_DATABASE",
			str(Path(__file__).with_name("message_stats.sqlite3")),
		)
		self.migration_forum_id = positive_int_env(
			"MESSAGE_STATS_MIGRATION_FORUM_ID", 1445647609342726194
		)
		self.migration_bot_user_id = positive_int_env(
			"MESSAGE_STATS_MIGRATION_BOT_ID", 1410698528627490818
		)
		self.stats = StatsDatabase(
			database_path, migration_forum_id=self.migration_forum_id
		)
		self.batch_size = positive_int_env("MESSAGE_STATS_BATCH_SIZE", 5000)
		self.import_concurrency = positive_int_env("MESSAGE_STATS_IMPORT_CONCURRENCY", 8)
		self.history_retry_attempts = positive_int_env(
			"MESSAGE_STATS_HISTORY_RETRY_ATTEMPTS", 5
		)
		self.migration_zone = migration_timezone()
		self.database_lock = asyncio.Lock()
		self.import_task: asyncio.Task | None = None

	async def db_call(self, operation, *args, **kwargs):
		async with self.database_lock:
			return await asyncio.to_thread(operation, *args, **kwargs)

	async def setup_hook(self):
		await self.add_cog(MessageCollector(self))

	async def on_ready(self):
		log.info("Logged in as %s", self.user)
		if self.import_task is None:
			self.import_task = asyncio.create_task(self.import_all_history())

	async def import_all_history(self):
		collector = self.get_cog("MessageCollector")
		if not isinstance(collector, MessageCollector):
			return
		for guild in self.guilds:
			try:
				locations, inserted, complete, inaccessible, failed = (
					await collector.import_guild_history(guild)
				)
				if failed:
					log.error(
						"History import pass incomplete guild=%s locations=%s complete=%s "
						"inaccessible=%s failed=%s inserted=%s; restart resumes checkpoints",
						guild.id,
						locations,
						complete,
						inaccessible,
						failed,
						inserted,
					)
				else:
					log.info(
						"Full accessible history import complete guild=%s locations=%s "
						"complete=%s inaccessible=%s inserted=%s",
						guild.id,
						locations,
						complete,
						inaccessible,
						inserted,
					)
			except Exception:
				log.exception("History import failed for guild %s", guild.id)

	async def close(self):
		if self.import_task and not self.import_task.done():
			self.import_task.cancel()
			await asyncio.gather(self.import_task, return_exceptions=True)
		await self.db_call(self.stats.close)
		await super().close()


def load_token() -> str:
	token = os.getenv("MESSAGE_STATS_BOT_TOKEN")
	if not token:
		raise RuntimeError("Set MESSAGE_STATS_BOT_TOKEN before starting the bot.")
	return token


if __name__ == "__main__":
	configure_event_loop()
	MessageStatsBot().run(load_token(), log_handler=None)
