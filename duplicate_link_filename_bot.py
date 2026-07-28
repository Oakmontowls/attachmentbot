"""Standalone duplicate detector using normalized URLs and perceptual image hashes."""

import asyncio
import logging
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import discord

from utils.hashing import phash_bytes


HISTORY_DAYS = 30
URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
TRAILING_URL_PUNCTUATION = ".,!?;:)]}'\""
TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
DISCORD_CDN_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}
DISCORD_CDN_PARAMETERS = {"ex", "is", "hm"}
IGNORED_LINK_DOMAINS = {"tenor.com", "tenor.googleapis.com", "klipy.com"}
IMAGE_EXTENSIONS = {
	".avif", ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg",
	".png", ".tif", ".tiff", ".webp",
}

log = logging.getLogger("duplicate-detector")
logging.basicConfig(
	level=logging.INFO,
	format="[%(asctime)s] [%(levelname)s] %(message)s",
	datefmt="%Y-%m-%d %H:%M:%S",
)


def normalize_url(value: str) -> str:
	value = value.strip().rstrip(TRAILING_URL_PUNCTUATION)
	try:
		parts = urlsplit(value)
		port = parts.port
	except ValueError:
		return value.casefold()
	scheme = parts.scheme.lower()
	hostname = (parts.hostname or "").lower()
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


def is_image_attachment(attachment: discord.Attachment) -> bool:
	if attachment.content_type and attachment.content_type.startswith("image/"):
		return True
	return Path(attachment.filename).suffix.casefold() in IMAGE_EXTENSIONS


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


def phash_distance(first: str, second: str) -> int:
	return (int(first, 16) ^ int(second, 16)).bit_count()


def phash_buckets(phash: str) -> list[tuple[int, str]]:
	# With eight buckets, hashes at distance <= 7 must share at least one bucket.
	return [(index, phash[index * 2:(index + 1) * 2]) for index in range(8)]


def message_link(guild_id: int, channel_id: int, message_id: int) -> str:
	return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


class DuplicateDatabase:
	def __init__(self, path: Path, distance_threshold: int):
		self.distance_threshold = distance_threshold
		self.conn = sqlite3.connect(path)
		self.conn.row_factory = sqlite3.Row
		self.conn.executescript(
			"""
			CREATE TABLE IF NOT EXISTS signatures (
				channel_id INTEGER NOT NULL,
				signature_type TEXT NOT NULL,
				signature TEXT NOT NULL,
				first_message_id INTEGER NOT NULL,
				first_author_id INTEGER NOT NULL,
				first_created_at TEXT NOT NULL,
				PRIMARY KEY (channel_id, signature_type, signature)
			);

			CREATE TABLE IF NOT EXISTS image_hashes (
				channel_id INTEGER NOT NULL,
				phash TEXT NOT NULL,
				first_message_id INTEGER NOT NULL,
				first_author_id INTEGER NOT NULL,
				first_created_at TEXT NOT NULL,
				PRIMARY KEY (channel_id, phash)
			);

			CREATE TABLE IF NOT EXISTS image_hash_buckets (
				channel_id INTEGER NOT NULL,
				bucket_index INTEGER NOT NULL,
				bucket_value TEXT NOT NULL,
				phash TEXT NOT NULL,
				PRIMARY KEY (channel_id, bucket_index, bucket_value, phash)
			);

			CREATE INDEX IF NOT EXISTS image_hash_bucket_lookup
			ON image_hash_buckets (channel_id, bucket_index, bucket_value);

			CREATE TABLE IF NOT EXISTS processed_messages (
				channel_id INTEGER NOT NULL,
				message_id INTEGER NOT NULL,
				PRIMARY KEY (channel_id, message_id)
			);

			CREATE TABLE IF NOT EXISTS duplicate_matches (
				guild_id INTEGER NOT NULL,
				channel_id INTEGER NOT NULL,
				message_id INTEGER NOT NULL,
				original_message_id INTEGER NOT NULL,
				signature_type TEXT NOT NULL,
				signature TEXT NOT NULL,
				historical INTEGER NOT NULL,
				distance INTEGER,
				PRIMARY KEY (channel_id, message_id, signature_type, signature)
			);

			CREATE TABLE IF NOT EXISTS scan_checkpoints (
				channel_id INTEGER PRIMARY KEY,
				last_message_id INTEGER NOT NULL
			);

			CREATE TABLE IF NOT EXISTS scan_metadata (
				channel_id INTEGER PRIMARY KEY,
				version TEXT NOT NULL
			);
			"""
		)
		columns = {
			row["name"]
			for row in self.conn.execute("PRAGMA table_info(duplicate_matches)").fetchall()
		}
		if "distance" not in columns:
			self.conn.execute("ALTER TABLE duplicate_matches ADD COLUMN distance INTEGER")
		self.conn.commit()

	def prepare_scan(self, channel_id: int):
		version = f"phash_{HISTORY_DAYS}d_v1"
		row = self.conn.execute(
			"SELECT version FROM scan_metadata WHERE channel_id = ?",
			(channel_id,),
		).fetchone()
		if row and row["version"].startswith(version):
			if row["version"] != version:
				self.conn.execute(
					"UPDATE scan_metadata SET version = ? WHERE channel_id = ?",
					(version, channel_id),
				)
				self.conn.commit()
			return

		# The previous filename index cannot serve as a pHash index.
		for table in (
			"signatures", "image_hashes", "image_hash_buckets",
			"processed_messages", "duplicate_matches", "scan_checkpoints",
		):
			self.conn.execute(f"DELETE FROM {table} WHERE channel_id = ?", (channel_id,))
		self.conn.execute(
			"""
			INSERT INTO scan_metadata (channel_id, version) VALUES (?, ?)
			ON CONFLICT(channel_id) DO UPDATE SET version = excluded.version
			""",
			(channel_id, version),
		)
		self.conn.commit()
		log.info("Initialized new %s index for channel %s", version, channel_id)

	def checkpoint(self, channel_id: int) -> int | None:
		row = self.conn.execute(
			"SELECT last_message_id FROM scan_checkpoints WHERE channel_id = ?",
			(channel_id,),
		).fetchone()
		return row["last_message_id"] if row else None

	def is_processed(self, channel_id: int, message_id: int) -> bool:
		return self.conn.execute(
			"SELECT 1 FROM processed_messages WHERE channel_id = ? AND message_id = ?",
			(channel_id, message_id),
		).fetchone() is not None

	def _find_phash_match(self, channel_id: int, phash: str, message_id: int):
		buckets = phash_buckets(phash)
		conditions = " OR ".join("(b.bucket_index = ? AND b.bucket_value = ?)" for _ in buckets)
		params = [channel_id, message_id]
		for bucket_index, bucket_value in buckets:
			params.extend((bucket_index, bucket_value))
		rows = self.conn.execute(
			f"""
			SELECT DISTINCT h.phash, h.first_message_id, h.first_author_id, h.first_created_at
			FROM image_hashes h
			JOIN image_hash_buckets b
			  ON b.channel_id = h.channel_id AND b.phash = h.phash
			WHERE b.channel_id = ? AND h.first_message_id != ? AND ({conditions})
			""",
			params,
		).fetchall()

		matches = [
			(phash_distance(phash, row["phash"]), row)
			for row in rows
			if phash_distance(phash, row["phash"]) <= self.distance_threshold
		]
		return min(matches, key=lambda item: (item[0], item[1]["first_message_id"])) if matches else None

	def _insert_phash(self, message: discord.Message, phash: str):
		result = self.conn.execute(
			"""
			INSERT OR IGNORE INTO image_hashes (
				channel_id, phash, first_message_id, first_author_id, first_created_at
			) VALUES (?, ?, ?, ?, ?)
			""",
			(
				message.channel.id, phash, message.id, message.author.id,
				message.created_at.isoformat(),
			),
		)
		if result.rowcount:
			self.conn.executemany(
				"""
				INSERT INTO image_hash_buckets (
					channel_id, bucket_index, bucket_value, phash
				) VALUES (?, ?, ?, ?)
				""",
				[
					(message.channel.id, index, value, phash)
					for index, value in phash_buckets(phash)
				],
			)

	def _save_duplicate(
		self,
		message: discord.Message,
		historical: bool,
		signature_type: str,
		signature: str,
		original_message_id: int,
		distance: int | None,
	):
		self.conn.execute(
			"""
			INSERT OR IGNORE INTO duplicate_matches (
				guild_id, channel_id, message_id, original_message_id,
				signature_type, signature, historical, distance
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
			""",
			(
				message.guild.id, message.channel.id, message.id, original_message_id,
				signature_type, signature, int(historical), distance,
			),
		)

	def process(
		self,
		message: discord.Message,
		historical: bool,
		image_hashes: set[str],
	):
		already_processed = self.conn.execute(
			"SELECT 1 FROM processed_messages WHERE channel_id = ? AND message_id = ?",
			(message.channel.id, message.id),
		).fetchone()
		if already_processed:
			return []

		matches = []
		for url in message_urls(message):
			original = self.conn.execute(
				"""
				SELECT first_message_id, first_author_id, first_created_at
				FROM signatures
				WHERE channel_id = ? AND signature_type = 'url' AND signature = ?
				""",
				(message.channel.id, url),
			).fetchone()
			if original:
				matches.append(("url", url, original, None))
				self._save_duplicate(
					message, historical, "url", url, original["first_message_id"], None,
				)
			else:
				self.conn.execute(
					"""
					INSERT INTO signatures (
						channel_id, signature_type, signature, first_message_id,
						first_author_id, first_created_at
					) VALUES (?, 'url', ?, ?, ?, ?)
					""",
					(
						message.channel.id, url, message.id, message.author.id,
						message.created_at.isoformat(),
					),
				)

		for phash in image_hashes:
			match = self._find_phash_match(message.channel.id, phash, message.id)
			if match:
				distance, original = match
				matches.append(("phash", phash, original, distance))
				self._save_duplicate(
					message, historical, "phash", phash,
					original["first_message_id"], distance,
				)
			self._insert_phash(message, phash)

		self.conn.execute(
			"INSERT INTO processed_messages (channel_id, message_id) VALUES (?, ?)",
			(message.channel.id, message.id),
		)
		return matches

	def save_checkpoint(self, channel_id: int, message_id: int):
		self.conn.execute(
			"""
			INSERT INTO scan_checkpoints (channel_id, last_message_id)
			VALUES (?, ?)
			ON CONFLICT(channel_id) DO UPDATE SET last_message_id = excluded.last_message_id
			WHERE excluded.last_message_id > scan_checkpoints.last_message_id
			""",
			(channel_id, message_id),
		)
		self.conn.commit()

	def commit(self):
		self.conn.commit()

	def saved_duplicates(self, channel_id: int) -> list[sqlite3.Row]:
		return self.conn.execute(
			"""
			SELECT guild_id, channel_id, message_id, original_message_id,
			       signature_type, signature, historical, distance
			FROM duplicate_matches
			WHERE channel_id = ?
			ORDER BY message_id, signature_type, signature
			""",
			(channel_id,),
		).fetchall()


class DuplicateDetector(discord.Client):
	def __init__(
		self,
		channel_id: int,
		database_path: Path,
		distance_threshold: int,
		reaction: str,
	):
		intents = discord.Intents.default()
		intents.guilds = True
		intents.messages = True
		intents.message_content = True
		super().__init__(intents=intents)
		self.channel_id = channel_id
		self.database = DuplicateDatabase(database_path, distance_threshold)
		self.reaction = discord.PartialEmoji.from_str(reaction)
		self.backfill_complete = asyncio.Event()
		self.queued_messages: dict[int, discord.Message] = {}
		self.backfill_task = None

	async def on_ready(self):
		log.info("Logged in as %s", self.user)
		if self.backfill_task is None:
			self.backfill_task = asyncio.create_task(self.backfill())

	async def _image_hashes(self, message: discord.Message) -> set[str]:
		hashes = set()
		loop = asyncio.get_running_loop()
		for attachment in message.attachments:
			if not is_image_attachment(attachment):
				continue
			try:
				image_bytes = await attachment.read(use_cached=True)
				phash = await loop.run_in_executor(None, phash_bytes, image_bytes)
				hashes.add(str(phash))
			except Exception as exc:
				log.warning(
					"[IMAGE SKIP] message=%s filename=%s error=%s",
					message.id, attachment.filename, exc,
				)
		return hashes

	async def on_message(self, message: discord.Message):
		if message.author.bot or message.channel.id != self.channel_id:
			return
		if not self.backfill_complete.is_set():
			self.queued_messages[message.id] = message
			return
		await self.process_new_message(message)

	async def process_new_message(self, message: discord.Message):
		matches = await self.process_and_log(message, historical=False)
		if any(signature_type == "phash" for signature_type, *_ in matches):
			try:
				await message.add_reaction(self.reaction)
			except discord.DiscordException as exc:
				log.warning("[REACTION ERROR] message=%s error=%s", message.id, exc)
		self.database.save_checkpoint(message.channel.id, message.id)

	def log_matches(self, message: discord.Message, matches, historical: bool):
		if not matches:
			return
		label = "HISTORICAL DUPLICATE" if historical else "NEW DUPLICATE"
		current_link = message_link(message.guild.id, message.channel.id, message.id)
		originals = defaultdict(list)
		for signature_type, signature, original, distance in matches:
			reason = f"{signature_type}={signature}"
			if distance is not None:
				reason += f" distance={distance}"
			originals[original["first_message_id"]].append(reason)
		for original_message_id, reasons in originals.items():
			original_link = message_link(message.guild.id, message.channel.id, original_message_id)
			log.warning(
				"[%s] message=%s original=%s matches=%s",
				label, current_link, original_link, " | ".join(reasons),
			)

	async def process_and_log(self, message: discord.Message, historical: bool):
		if self.database.is_processed(message.channel.id, message.id):
			return []
		hashes = await self._image_hashes(message)
		matches = self.database.process(message, historical, hashes)
		self.log_matches(message, matches, historical)
		return matches

	def report_saved_duplicates(self):
		rows = self.database.saved_duplicates(self.channel_id)
		if not rows:
			return
		log.info("Previously saved duplicate matches: %d", len(rows))
		for row in rows:
			label = "HISTORICAL DUPLICATE" if row["historical"] else "NEW DUPLICATE"
			distance = f" distance={row['distance']}" if row["distance"] is not None else ""
			log.warning(
				"[%s] message=%s original=%s matches=%s=%s%s",
				label,
				message_link(row["guild_id"], row["channel_id"], row["message_id"]),
				message_link(row["guild_id"], row["channel_id"], row["original_message_id"]),
				row["signature_type"], row["signature"], distance,
			)

	async def backfill(self):
		try:
			channel = self.get_channel(self.channel_id) or await self.fetch_channel(self.channel_id)
			if not hasattr(channel, "history"):
				raise RuntimeError(f"Channel {self.channel_id} does not provide message history")

			self.database.prepare_scan(self.channel_id)
			cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)
			cutoff_id = discord.utils.time_snowflake(cutoff, high=False)
			checkpoint = self.database.checkpoint(self.channel_id)
			after_id = max(cutoff_id, checkpoint or 0)
			after = discord.Object(id=after_id)
			if checkpoint:
				log.info("Checking channel %s for messages after checkpoint %s", self.channel_id, checkpoint)
			else:
				log.info(
					"Starting historical scan for channel %s after %s (last %d days)",
					self.channel_id, cutoff.isoformat(), HISTORY_DAYS,
				)

			processed = 0
			last_message_id = checkpoint
			async for message in channel.history(limit=None, after=after, oldest_first=True):
				if not message.author.bot:
					await self.process_and_log(message, historical=True)
				processed += 1
				last_message_id = message.id
				if processed % 100 == 0:
					self.database.save_checkpoint(self.channel_id, last_message_id)
					log.info("Historical scan progress: %d messages this run", processed)

			if last_message_id:
				self.database.save_checkpoint(self.channel_id, last_message_id)
			else:
				self.database.commit()

			for message in sorted(self.queued_messages.values(), key=lambda item: item.created_at):
				await self.process_new_message(message)
			self.queued_messages.clear()
			self.backfill_complete.set()
			log.info("Historical scan complete: %d messages processed this run", processed)
		except Exception:
			log.exception("Historical scan failed")


def required_environment(name: str) -> str:
	value = os.getenv(name, "").strip()
	if not value:
		raise RuntimeError(f"Set the {name} environment variable before starting the bot")
	return value


if __name__ == "__main__":
	token = required_environment("DUPLICATE_BOT_TOKEN")
	channel_id = int(required_environment("DUPLICATE_CHANNEL_ID"))
	database_path = Path(os.getenv("DUPLICATE_DATABASE", "duplicate_detector.sqlite3"))
	distance_threshold = int(os.getenv("DUPLICATE_PHASH_DISTANCE", "0"))
	if not 0 <= distance_threshold <= 7:
		raise RuntimeError("DUPLICATE_PHASH_DISTANCE must be between 0 and 7")
	reaction = os.getenv("DUPLICATE_REACTION", "\u267b\ufe0f").strip()
	if not reaction:
		raise RuntimeError("DUPLICATE_REACTION cannot be empty")
	DuplicateDetector(channel_id, database_path, distance_threshold, reaction).run(token)
