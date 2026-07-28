import asyncio
import re
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import discord

from core.database import db
from core.logger import log
from utils.hashing import phash_bytes


PHASH_DISTANCE = 0
RECYCLE_REACTION = discord.PartialEmoji.from_str("\u267b\ufe0f")
URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
TRAILING_URL_PUNCTUATION = ".,!?;:)]}'\""
TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
DISCORD_CDN_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}
DISCORD_CDN_PARAMETERS = {"ex", "is", "hm"}
IGNORED_LINK_DOMAINS = {
	"tenor.com", "tenor.googleapis.com", "klipy.com", "giphy.com", "gph.is",
}
TWITTER_DOMAINS = {
	"twitter.com", "x.com", "fxtwitter.com", "fixupx.com", "twittpr.com",
	"vxtwitter.com", "fixvx.com",
}
INSTAGRAM_DOMAINS = {
	"instagram.com", "ddinstagram.com", "kkinstagram.com", "instagramez.com",
	"fxinstagram.com", "fxig.seria.moe",
}
TWITTER_STATUS_PATTERN = re.compile(r"/(?:status|statuses)/(\d+)", re.IGNORECASE)
INSTAGRAM_POST_PATTERN = re.compile(r"^/(?:p|reel|reels|tv)/([^/]+)", re.IGNORECASE)
URL_NORMALIZATION_VERSION = "social_posts_v2_latest"
IMAGE_EXTENSIONS = {
	".avif", ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg",
	".png", ".tif", ".tiff", ".webp",
}
VIDEO_EXTENSIONS = {
	".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm",
}
VIDEO_HASH_TIMEOUT_SECONDS = 60


def matches_domain(hostname: str, domains: set[str]) -> bool:
	return any(hostname == domain or hostname.endswith(f".{domain}") for domain in domains)


def normalize_url(value: str) -> str:
	value = value.strip().rstrip(TRAILING_URL_PUNCTUATION)
	try:
		parts = urlsplit(value)
		port = parts.port
	except ValueError:
		return value.casefold()
	scheme = parts.scheme.lower()
	hostname = (parts.hostname or "").lower()
	twitter_url = matches_domain(hostname, TWITTER_DOMAINS)
	instagram_url = matches_domain(hostname, INSTAGRAM_DOMAINS)
	if twitter_url:
		match = TWITTER_STATUS_PATTERN.search(parts.path)
		if match:
			return f"https://x.com/i/status/{match.group(1)}"
		hostname = "x.com"
	elif instagram_url:
		match = INSTAGRAM_POST_PATTERN.match(parts.path)
		if match:
			return f"https://instagram.com/p/{match.group(1)}"
		hostname = "instagram.com"

	if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
		netloc = f"{hostname}:{port}"
	else:
		netloc = hostname

	path = parts.path or "/"
	if path != "/":
		path = path.rstrip("/")
	query = []
	for key, item_value in parse_qsl(parts.query, keep_blank_values=True):
		lower_key = key.lower()
		if lower_key.startswith("utm_") or lower_key in TRACKING_PARAMETERS:
			continue
		if hostname in DISCORD_CDN_HOSTS and lower_key in DISCORD_CDN_PARAMETERS:
			continue
		query.append((key, item_value))
	return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))


def is_ignored_url(value: str) -> bool:
	hostname = (urlsplit(value).hostname or "").lower()
	return any(
		hostname == domain or hostname.endswith(f".{domain}")
		for domain in IGNORED_LINK_DOMAINS
	)


def message_urls(message: discord.Message) -> set[str]:
	return {
		normalized
		for match in URL_PATTERN.finditer(message.content)
		if (normalized := normalize_url(match.group(0))) and not is_ignored_url(normalized)
	}


def is_image_attachment(attachment: discord.Attachment) -> bool:
	if attachment.content_type and attachment.content_type.startswith("image/"):
		return True
	return Path(attachment.filename).suffix.casefold() in IMAGE_EXTENSIONS


def is_video_attachment(attachment: discord.Attachment) -> bool:
	if attachment.content_type and attachment.content_type.startswith("video/"):
		return True
	return Path(attachment.filename).suffix.casefold() in VIDEO_EXTENSIONS


def split_media_hash(value: str) -> tuple[str, str]:
	kind, separator, digest = value.partition(":")
	return (kind, digest) if separator else ("image", value)


def phash_distance(first: str, second: str) -> int:
	first_kind, first_digest = split_media_hash(first)
	second_kind, second_digest = split_media_hash(second)
	if first_kind != second_kind:
		return 65
	return (int(first_digest, 16) ^ int(second_digest, 16)).bit_count()


def phash_buckets(phash: str) -> list[tuple[int, str]]:
	kind, digest = split_media_hash(phash)
	prefix = "video:" if kind == "video" else ""
	return [
		(index, prefix + digest[index * 2:(index + 1) * 2])
		for index in range(8)
	]


def is_reply_message(message: discord.Message) -> bool:
	return message.reference is not None


def history_cutoff_id(days: int) -> int:
	cutoff = datetime.now(timezone.utc) - timedelta(days=days)
	return discord.utils.time_snowflake(cutoff, high=False)


async def video_frame_bytes(video_bytes: bytes) -> bytes:
	with tempfile.TemporaryDirectory(prefix="attachmentbot-video-") as temp_dir:
		input_path = Path(temp_dir) / "input.video"
		await asyncio.to_thread(input_path.write_bytes, video_bytes)
		try:
			process = await asyncio.create_subprocess_exec(
				"ffmpeg",
				"-hide_banner",
				"-loglevel", "error",
				"-i", str(input_path),
				"-vf", "thumbnail=100,scale=320:-2",
				"-frames:v", "1",
				"-f", "image2pipe",
				"-vcodec", "png",
				"pipe:1",
				stdin=asyncio.subprocess.DEVNULL,
				stdout=asyncio.subprocess.PIPE,
				stderr=asyncio.subprocess.PIPE,
			)
		except FileNotFoundError as exc:
			raise RuntimeError("ffmpeg is not installed or is not on PATH") from exc

		try:
			stdout, stderr = await asyncio.wait_for(
				process.communicate(),
				timeout=VIDEO_HASH_TIMEOUT_SECONDS,
			)
		except asyncio.TimeoutError as exc:
			process.kill()
			await process.communicate()
			raise RuntimeError(
				f"ffmpeg exceeded the {VIDEO_HASH_TIMEOUT_SECONDS}-second timeout"
			) from exc
		if process.returncode or not stdout:
			error = stderr.decode("utf-8", errors="replace").strip()
			raise RuntimeError(error or f"ffmpeg exited with status {process.returncode}")
		return stdout


def jump_url(message: discord.Message, original_message_id: int | None = None) -> str:
	message_id = original_message_id or message.id
	return f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message_id}"


class RecycleService:
	def __init__(self):
		self.bot = None
		self._ready_guilds = set()
		self._initializing = set()
		self._queued_messages = defaultdict(dict)
		self._locks = defaultdict(asyncio.Lock)
		self._generation = defaultdict(int)
		self._tasks = set()
		self._url_migration_lock = asyncio.Lock()

	def _schedule(self, guild: discord.Guild):
		if guild.id in self._initializing:
			return
		task = asyncio.create_task(self.initialize_guild(guild))
		self._tasks.add(task)
		task.add_done_callback(self._tasks.discard)

	async def start(self, bot):
		self.bot = bot
		await bot.wait_until_ready()
		for guild in bot.guilds:
			settings = db.get_recycle_settings(guild.id)
			if settings["enabled"] and settings["channel_id"]:
				self._schedule(guild)

	async def stop(self):
		for guild_id in set(self._generation) | self._ready_guilds | self._initializing:
			self._generation[guild_id] += 1
		for task in list(self._tasks):
			task.cancel()
		if self._tasks:
			await asyncio.gather(*self._tasks, return_exceptions=True)
		self._tasks.clear()
		self._ready_guilds.clear()
		self._initializing.clear()
		self._queued_messages.clear()
		self.bot = None

	async def set_enabled(self, guild: discord.Guild, enabled: bool):
		settings = db.get_recycle_settings(guild.id)
		if enabled and not settings["channel_id"]:
			raise ValueError("Set a recycle channel before enabling the module.")
		db.set_recycle_enabled(guild.id, enabled)
		self._generation[guild.id] += 1
		self._ready_guilds.discard(guild.id)
		if enabled:
			self._schedule(guild)
		else:
			self._queued_messages.pop(guild.id, None)

	def set_reply_fallback(self, guild_id: int, enabled: bool):
		db.set_recycle_reply_fallback(guild_id, enabled)

	def set_ignore_replies(self, guild_id: int, enabled: bool):
		db.set_recycle_ignore_replies(guild_id, enabled)

	async def set_history_days(self, guild: discord.Guild, days: int) -> bool:
		if not db.set_recycle_history_days(guild.id, days):
			return False
		self._generation[guild.id] += 1
		self._ready_guilds.discard(guild.id)
		if db.get_recycle_settings(guild.id)["enabled"]:
			self._schedule(guild)
		return True

	async def set_channel(self, guild: discord.Guild, channel: discord.TextChannel):
		settings = db.get_recycle_settings(guild.id)
		changed = settings["channel_id"] != channel.id
		db.set_recycle_channel(guild.id, channel.id)
		if not changed:
			return
		self._generation[guild.id] += 1
		self._ready_guilds.discard(guild.id)
		self._queued_messages.pop(guild.id, None)
		if settings["enabled"]:
			self._schedule(guild)

	async def handle_message(self, message: discord.Message):
		if message.author.bot or not message.guild:
			return
		settings = db.get_recycle_settings(message.guild.id)
		if not settings["enabled"] or message.channel.id != settings["channel_id"]:
			return

		guild_id = message.guild.id
		if guild_id not in self._ready_guilds:
			self._queued_messages[guild_id][message.id] = message
			self._schedule(message.guild)
			return

		async with self._locks[guild_id]:
			generation = self._generation[guild_id]
			await self._process_message(message, historical=False, generation=generation)

	def _is_processed(self, message: discord.Message) -> bool:
		return db.conn.execute(
			"""
			SELECT 1 FROM recycle_processed_messages
			WHERE guild_id = ? AND channel_id = ? AND message_id = ?
			""",
			(message.guild.id, message.channel.id, message.id),
		).fetchone() is not None

	async def _migrate_url_signatures(self, guild_id: int, channel_id: int):
		metadata_key = f"recycle_url_normalization:{guild_id}:{channel_id}"
		if db.get_metadata(metadata_key) == URL_NORMALIZATION_VERSION:
			return

		async with self._url_migration_lock:
			if db.get_metadata(metadata_key) == URL_NORMALIZATION_VERSION:
				return
			rows = db.conn.execute(
				"""
				SELECT signature, first_message_id
				FROM recycle_url_signatures
				WHERE guild_id = ? AND channel_id = ?
				""",
				(guild_id, channel_id),
			).fetchall()
			canonical = {}
			for row in rows:
				signature = normalize_url(row["signature"])
				canonical[signature] = max(
					canonical.get(signature, row["first_message_id"]),
					row["first_message_id"],
				)

			with db.conn:
				db.conn.execute(
					"DELETE FROM recycle_url_signatures WHERE guild_id = ? AND channel_id = ?",
					(guild_id, channel_id),
				)
				db.conn.executemany(
					"""
					INSERT INTO recycle_url_signatures (
						guild_id, channel_id, signature, first_message_id
					) VALUES (?, ?, ?, ?)
					""",
					[
						(guild_id, channel_id, signature, message_id)
						for signature, message_id in canonical.items()
					],
				)
			db.set_metadata(metadata_key, URL_NORMALIZATION_VERSION)
			log.info(
				f"[RECYCLE URL MIGRATION] guild={guild_id} channel={channel_id} "
				f"before={len(rows)} after={len(canonical)}"
			)

	async def _media_hashes(self, message: discord.Message) -> set[str]:
		hashes = set()
		loop = asyncio.get_running_loop()
		for attachment in message.attachments:
			is_image = is_image_attachment(attachment)
			is_video = is_video_attachment(attachment)
			if not is_image and not is_video:
				continue
			try:
				media_bytes = await attachment.read(use_cached=True)
				if is_video:
					frame_bytes = await video_frame_bytes(media_bytes)
					phash = await loop.run_in_executor(None, phash_bytes, frame_bytes)
					hashes.add(f"video:{phash}")
				else:
					phash = await loop.run_in_executor(None, phash_bytes, media_bytes)
					hashes.add(str(phash))
			except Exception as exc:
				log.info(
					f"[RECYCLE MEDIA SKIP] message={message.id} "
					f"filename={attachment.filename} error={exc}"
				)
		return hashes

	def _find_phash_match(self, message: discord.Message, phash: str, cutoff_id: int):
		buckets = phash_buckets(phash)
		conditions = " OR ".join(
			"(b.bucket_index = ? AND b.bucket_value = ?)" for _ in buckets
		)
		params = [message.guild.id, message.channel.id, message.id, cutoff_id]
		for bucket_index, bucket_value in buckets:
			params.extend((bucket_index, bucket_value))
		rows = db.conn.execute(
			f"""
			SELECT DISTINCT h.phash, h.first_message_id
			FROM recycle_image_hashes h
			JOIN recycle_image_hash_buckets b
			  ON b.guild_id = h.guild_id
			 AND b.channel_id = h.channel_id
			 AND b.phash = h.phash
			WHERE b.guild_id = ? AND b.channel_id = ?
			  AND h.first_message_id != ? AND h.first_message_id >= ?
			  AND ({conditions})
			""",
			params,
		).fetchall()
		matches = []
		for row in rows:
			distance = phash_distance(phash, row["phash"])
			if distance <= PHASH_DISTANCE:
				matches.append((distance, row["first_message_id"]))
		return min(matches) if matches else None

	def _insert_phash(self, message: discord.Message, phash: str):
		db.conn.execute(
			"""
			INSERT INTO recycle_image_hashes (
				guild_id, channel_id, phash, first_message_id
			) VALUES (?, ?, ?, ?)
			ON CONFLICT(guild_id, channel_id, phash)
			DO UPDATE SET first_message_id = excluded.first_message_id
			""",
			(message.guild.id, message.channel.id, phash, message.id),
		)
		db.conn.executemany(
			"""
			INSERT OR IGNORE INTO recycle_image_hash_buckets (
				guild_id, channel_id, bucket_index, bucket_value, phash
			) VALUES (?, ?, ?, ?, ?)
			""",
			[
				(message.guild.id, message.channel.id, index, value, phash)
				for index, value in phash_buckets(phash)
			],
		)

	def _store_signatures(
		self,
		message: discord.Message,
		hashes: set[str],
		cutoff_id: int,
	):
		matches = []
		for url in message_urls(message):
			row = db.conn.execute(
				"""
				SELECT first_message_id FROM recycle_url_signatures
				WHERE guild_id = ? AND channel_id = ? AND signature = ?
				""",
				(message.guild.id, message.channel.id, url),
			).fetchone()
			if row and row["first_message_id"] >= cutoff_id:
				matches.append(("url", url, row["first_message_id"], None))
			db.conn.execute(
				"""
				INSERT INTO recycle_url_signatures (
					guild_id, channel_id, signature, first_message_id
				) VALUES (?, ?, ?, ?)
				ON CONFLICT(guild_id, channel_id, signature)
				DO UPDATE SET first_message_id = excluded.first_message_id
				""",
				(message.guild.id, message.channel.id, url, message.id),
			)

		for phash in hashes:
			match = self._find_phash_match(message, phash, cutoff_id)
			if match:
				distance, original_message_id = match
				matches.append(("phash", phash, original_message_id, distance))
			self._insert_phash(message, phash)

		db.conn.execute(
			"""
			INSERT INTO recycle_processed_messages (guild_id, channel_id, message_id)
			VALUES (?, ?, ?)
			""",
			(message.guild.id, message.channel.id, message.id),
		)
		return matches

	async def _mark_duplicate(self, message: discord.Message):
		try:
			await message.add_reaction(RECYCLE_REACTION)
			return
		except discord.DiscordException as exc:
			log.info(f"[RECYCLE REACTION ERROR] message={message.id} error={exc}")

		if not db.get_recycle_settings(message.guild.id)["reply_fallback"]:
			return
		try:
			await message.reply(
				f"_{RECYCLE_REACTION}_",
				mention_author=True,
				allowed_mentions=discord.AllowedMentions(
					everyone=False,
					users=False,
					roles=False,
					replied_user=True,
				),
			)
		except discord.DiscordException as exc:
			log.info(f"[RECYCLE REPLY ERROR] message={message.id} error={exc}")

	async def _process_message(self, message: discord.Message, historical: bool, generation: int):
		settings = db.get_recycle_settings(message.guild.id)
		ignore_current_match = settings["ignore_replies"] and is_reply_message(message)
		if self._is_processed(message):
			return
		hashes = await self._media_hashes(message)
		if generation != self._generation[message.guild.id]:
			return

		cutoff_id = history_cutoff_id(settings["history_days"])
		matches = self._store_signatures(message, hashes, cutoff_id)
		if not ignore_current_match:
			for signature_type, signature, original_message_id, distance in matches:
				distance_text = f" distance={distance}" if distance is not None else ""
				label = "HISTORICAL" if historical else "NEW"
				log.info(
					f"[RECYCLE {label} DUPLICATE] message={jump_url(message)} "
					f"original={jump_url(message, original_message_id)} "
					f"match={signature_type}:{signature}{distance_text}"
				)

		if not historical and matches and not ignore_current_match:
			await self._mark_duplicate(message)

		if not historical:
			db.update_recycle_checkpoint(message.guild.id, message.id)

	async def initialize_guild(self, guild: discord.Guild):
		if guild.id in self._initializing:
			return
		self._initializing.add(guild.id)
		generation = self._generation[guild.id]
		try:
			settings = db.get_recycle_settings(guild.id)
			channel = guild.get_channel(settings["channel_id"]) if settings["channel_id"] else None
			if not settings["enabled"] or not isinstance(channel, discord.TextChannel):
				return
			await self._migrate_url_signatures(guild.id, channel.id)

			cutoff = datetime.now(timezone.utc) - timedelta(days=settings["history_days"])
			cutoff_id = discord.utils.time_snowflake(cutoff, high=False)
			after_id = max(cutoff_id, settings["checkpoint_message_id"] or 0)
			processed = 0
			last_message_id = settings["checkpoint_message_id"]
			log.info(
				f"[RECYCLE SCAN] guild={guild.id} channel={channel.id} "
				f"history_days={settings['history_days']} starting_after={after_id}"
			)
			async for message in channel.history(
				limit=None,
				after=discord.Object(id=after_id),
				oldest_first=True,
			):
				if generation != self._generation[guild.id]:
					return
				if not message.author.bot:
					await self._process_message(message, historical=True, generation=generation)
				processed += 1
				last_message_id = message.id
				if processed % 100 == 0:
					db.update_recycle_checkpoint(guild.id, last_message_id)
					log.info(f"[RECYCLE SCAN] guild={guild.id} processed={processed}")

			if last_message_id:
				db.update_recycle_checkpoint(guild.id, last_message_id)

			async with self._locks[guild.id]:
				while self._queued_messages[guild.id]:
					queued = sorted(
						self._queued_messages[guild.id].values(),
						key=lambda item: item.created_at,
					)
					self._queued_messages[guild.id].clear()
					for message in queued:
						await self._process_message(message, historical=False, generation=generation)
				self._ready_guilds.add(guild.id)
			log.info(f"[RECYCLE READY] guild={guild.id} processed={processed}")
		except Exception as exc:
			log.info(f"[RECYCLE SCAN ERROR] guild={guild.id} error={exc}")
		finally:
			self._initializing.discard(guild.id)
			settings = db.get_recycle_settings(guild.id)
			if (
				settings["enabled"]
				and settings["channel_id"]
				and guild.id not in self._ready_guilds
				and generation != self._generation[guild.id]
			):
				self._schedule(guild)


recycle_service = RecycleService()
