import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def cutoff_timestamp(days: int | None) -> int | None:
	if days is None:
		return None
	return int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())


class StatsDatabase:
	"""Stores message events without storing message content."""

	def __init__(self, path: str | Path, *, migration_forum_id: int = 1445647609342726194):
		self.path = Path(path)
		self.migration_forum_id = migration_forum_id
		self.conn = sqlite3.connect(self.path, check_same_thread=False)
		self.conn.row_factory = sqlite3.Row
		self.conn.execute("PRAGMA journal_mode = WAL")
		self.conn.execute("PRAGMA synchronous = NORMAL")
		self.conn.execute("PRAGMA foreign_keys = ON")
		self.setup()

	def setup(self):
		self.conn.executescript(
			"""
			CREATE TABLE IF NOT EXISTS guilds (
				guild_id INTEGER PRIMARY KEY,
				name TEXT NOT NULL,
				updated_at INTEGER NOT NULL
			);

			CREATE TABLE IF NOT EXISTS members (
				guild_id INTEGER NOT NULL,
				user_id INTEGER NOT NULL,
				username TEXT NOT NULL,
				display_name TEXT NOT NULL,
				avatar_url TEXT,
				updated_at INTEGER NOT NULL,
				PRIMARY KEY (guild_id, user_id)
			);

			CREATE TABLE IF NOT EXISTS channels (
				channel_id INTEGER PRIMARY KEY,
				guild_id INTEGER NOT NULL,
				parent_channel_id INTEGER,
				name TEXT NOT NULL,
				kind TEXT NOT NULL,
				owner_id INTEGER,
				created_at INTEGER NOT NULL,
				archived INTEGER NOT NULL DEFAULT 0,
				updated_at INTEGER NOT NULL
			);

			CREATE INDEX IF NOT EXISTS channels_guild_parent
			ON channels (guild_id, parent_channel_id);

			CREATE TABLE IF NOT EXISTS messages (
				message_id INTEGER PRIMARY KEY,
				guild_id INTEGER NOT NULL,
				channel_id INTEGER NOT NULL,
				parent_channel_id INTEGER,
				user_id INTEGER NOT NULL,
				created_at INTEGER NOT NULL,
				FOREIGN KEY (channel_id) REFERENCES channels(channel_id)
					ON DELETE CASCADE
			);

			CREATE INDEX IF NOT EXISTS messages_guild_time
			ON messages (guild_id, created_at);

			CREATE INDEX IF NOT EXISTS messages_channel_time
			ON messages (guild_id, channel_id, created_at);

			CREATE INDEX IF NOT EXISTS messages_parent_time
			ON messages (guild_id, parent_channel_id, created_at);

			CREATE INDEX IF NOT EXISTS messages_user_time
			ON messages (guild_id, user_id, created_at);

			CREATE TABLE IF NOT EXISTS stats_metadata (
				key TEXT PRIMARY KEY,
				value TEXT NOT NULL
			);

			CREATE TABLE IF NOT EXISTS history_imports (
				channel_id INTEGER PRIMARY KEY,
				guild_id INTEGER NOT NULL,
				after_message_id INTEGER,
				migration_user_id INTEGER,
				migration_timestamp INTEGER,
				completed_at INTEGER,
				updated_at INTEGER NOT NULL
			);

			CREATE INDEX IF NOT EXISTS history_imports_guild
			ON history_imports (guild_id, completed_at);

			CREATE TABLE IF NOT EXISTS daily_message_stats (
				guild_id INTEGER NOT NULL,
				channel_id INTEGER NOT NULL,
				parent_channel_id INTEGER,
				user_id INTEGER NOT NULL,
				day TEXT NOT NULL,
				message_count INTEGER NOT NULL,
				PRIMARY KEY (guild_id, channel_id, user_id, day)
			);

			CREATE INDEX IF NOT EXISTS daily_stats_guild_day
			ON daily_message_stats (guild_id, day);

			CREATE INDEX IF NOT EXISTS daily_stats_parent_day
			ON daily_message_stats (guild_id, parent_channel_id, day);

			CREATE INDEX IF NOT EXISTS daily_stats_user_day
			ON daily_message_stats (guild_id, user_id, day);

			CREATE TRIGGER IF NOT EXISTS messages_daily_stats_insert
			AFTER INSERT ON messages
			BEGIN
				INSERT INTO daily_message_stats (
					guild_id, channel_id, parent_channel_id, user_id, day, message_count
				) VALUES (
					NEW.guild_id, NEW.channel_id, NEW.parent_channel_id, NEW.user_id,
					date(NEW.created_at, 'unixepoch'), 1
				)
				ON CONFLICT(guild_id, channel_id, user_id, day) DO UPDATE SET
					message_count = message_count + 1;
			END;

			CREATE TRIGGER IF NOT EXISTS messages_daily_stats_delete
			AFTER DELETE ON messages
			BEGIN
				UPDATE daily_message_stats
				SET message_count = message_count - 1
				WHERE guild_id = OLD.guild_id
					AND channel_id = OLD.channel_id
					AND user_id = OLD.user_id
					AND day = date(OLD.created_at, 'unixepoch');
				DELETE FROM daily_message_stats WHERE message_count <= 0;
			END;
			"""
		)
		self.ensure_history_import_columns()
		self.initialize_daily_stats()
		self.initialize_forum_reattribution()
		self.conn.execute("PRAGMA optimize")
		self.conn.commit()

	def ensure_history_import_columns(self):
		columns = {
			row["name"] for row in self.conn.execute("PRAGMA table_info(history_imports)")
		}
		for name in ("migration_user_id", "migration_timestamp"):
			if name not in columns:
				self.conn.execute(f"ALTER TABLE history_imports ADD COLUMN {name} INTEGER")

	def initialize_daily_stats(self):
		row = self.conn.execute(
			"SELECT value FROM stats_metadata WHERE key = 'daily_stats_version'"
		).fetchone()
		if row and row["value"] == "1":
			return
		self.conn.execute("DELETE FROM daily_message_stats")
		self.conn.execute(
			"""
			INSERT INTO daily_message_stats (
				guild_id, channel_id, parent_channel_id, user_id, day, message_count
			)
			SELECT guild_id, channel_id, parent_channel_id, user_id,
				date(created_at, 'unixepoch'), COUNT(*)
			FROM messages
			GROUP BY guild_id, channel_id, parent_channel_id, user_id,
				date(created_at, 'unixepoch')
			"""
		)
		self.conn.execute(
			"""
			INSERT INTO stats_metadata (key, value) VALUES ('daily_stats_version', '1')
			ON CONFLICT(key) DO UPDATE SET value = excluded.value
			"""
		)

	def initialize_forum_reattribution(self):
		"""Schedule one forum rescan after migration-embed support is installed."""
		version = "5"
		row = self.conn.execute(
			"SELECT value FROM stats_metadata WHERE key = 'forum_reattribution_version'"
		).fetchone()
		if row and row["value"] == version:
			return
		# Negative IDs are reserved for migrated embed records. Rebuild them so
		# older broad-parser versions cannot leave reattributed rows out of scope.
		self.conn.execute("DELETE FROM messages WHERE message_id < 0")
		self.conn.execute(
			"""
			DELETE FROM history_imports
			WHERE channel_id IN (
				SELECT channel_id
				FROM channels
				WHERE kind = 'forum_post' AND parent_channel_id = ?
			)
			""",
			(self.migration_forum_id,),
		)
		self.conn.execute(
			"""
			INSERT INTO stats_metadata (key, value)
			VALUES ('forum_reattribution_version', ?)
			ON CONFLICT(key) DO UPDATE SET value = excluded.value
			""",
			(version,),
		)

	def upsert_guild(self, guild_id: int, name: str):
		self.conn.execute(
			"""
			INSERT INTO guilds (guild_id, name, updated_at)
			VALUES (?, ?, ?)
			ON CONFLICT(guild_id) DO UPDATE SET
				name = excluded.name,
				updated_at = excluded.updated_at
			""",
			(guild_id, name, int(datetime.now(timezone.utc).timestamp())),
		)
		self.conn.commit()

	def close(self):
		self.conn.close()

	def upsert_channel(
		self,
		*,
		guild_id: int,
		channel_id: int,
		parent_channel_id: int | None,
		name: str,
		kind: str,
		owner_id: int | None,
		created_at: int,
		archived: bool,
	):
		now = int(datetime.now(timezone.utc).timestamp())
		self.conn.execute(
			"""
			INSERT INTO channels (
				channel_id, guild_id, parent_channel_id, name, kind, owner_id,
				created_at, archived, updated_at
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(channel_id) DO UPDATE SET
				guild_id = excluded.guild_id,
				parent_channel_id = excluded.parent_channel_id,
				name = excluded.name,
				kind = excluded.kind,
				owner_id = COALESCE(excluded.owner_id, channels.owner_id),
				archived = excluded.archived,
				updated_at = excluded.updated_at
			""",
			(
				channel_id,
				guild_id,
				parent_channel_id,
				name,
				kind,
				owner_id,
				created_at,
				int(archived),
				now,
			),
		)
		self.conn.commit()

	def record_message(
		self,
		*,
		message_id: int,
		guild_id: int,
		channel_id: int,
		parent_channel_id: int | None,
		user_id: int,
		created_at: int,
		username: str | None = None,
		display_name: str | None = None,
		avatar_url: str | None = None,
	) -> bool:
		if username is not None:
			self._upsert_members([
				(
					guild_id,
					user_id,
					username,
					display_name or username,
					avatar_url,
					int(datetime.now(timezone.utc).timestamp()),
				)
			])
		result = self.conn.execute(
			"""
			INSERT OR IGNORE INTO messages (
				message_id, guild_id, channel_id, parent_channel_id,
				user_id, created_at
			) VALUES (?, ?, ?, ?, ?, ?)
			""",
			(
				message_id,
				guild_id,
				channel_id,
				parent_channel_id,
				user_id,
				created_at,
			),
		)
		self.conn.commit()
		return result.rowcount > 0

	def _upsert_members(
		self,
		members: list[tuple[int, int, str, str, str | None, int]],
	):
		if not members:
			return
		self.conn.executemany(
			"""
			INSERT INTO members (
				guild_id, user_id, username, display_name, avatar_url, updated_at
			) VALUES (?, ?, ?, ?, ?, ?)
			ON CONFLICT(guild_id, user_id) DO UPDATE SET
				username = excluded.username,
				display_name = excluded.display_name,
				avatar_url = excluded.avatar_url,
				updated_at = excluded.updated_at
			""",
			members,
		)

	def record_messages(
		self,
		messages: list[tuple[int, int, int, int | None, int, int]],
		members: list[tuple[int, int, str, str, str | None, int]] | None = None,
	) -> int:
		"""Insert a history batch in one transaction and return the new row count."""
		if not messages:
			return 0
		self._upsert_members(members or [])
		result = self.conn.executemany(
			"""
			INSERT OR IGNORE INTO messages (
				message_id, guild_id, channel_id, parent_channel_id,
				user_id, created_at
			) VALUES (?, ?, ?, ?, ?, ?)
			""",
			messages,
		)
		self.conn.commit()
		return result.rowcount

	def latest_message_id(self, guild_id: int, channel_id: int) -> int | None:
		row = self.conn.execute(
			"""
			SELECT MAX(message_id) AS message_id
			FROM messages
			WHERE guild_id = ? AND channel_id = ?
			""",
			(guild_id, channel_id),
		).fetchone()
		return row["message_id"] if row else None

	def history_cursor(self, guild_id: int, channel_id: int) -> int | None:
		"""Return the last message traversed by the history importer.

		This deliberately ignores live message rows. A live event can arrive before
		the importer reaches a channel, so MAX(messages.message_id) is not proof that
		the older history was scanned.
		"""
		row = self.conn.execute(
			"""
			SELECT after_message_id
			FROM history_imports
			WHERE guild_id = ? AND channel_id = ?
			""",
			(guild_id, channel_id),
		).fetchone()
		return row["after_message_id"] if row else None

	def history_state(
		self, guild_id: int, channel_id: int
	) -> tuple[int | None, int | None, int | None]:
		row = self.conn.execute(
			"""
			SELECT after_message_id, migration_user_id, migration_timestamp
			FROM history_imports
			WHERE guild_id = ? AND channel_id = ?
			""",
			(guild_id, channel_id),
		).fetchone()
		if row is None:
			return None, None, None
		return (
			row["after_message_id"],
			row["migration_user_id"],
			row["migration_timestamp"],
		)

	def history_import_statuses(
		self, guild_id: int
	) -> dict[int, tuple[int | None, int | None]]:
		return {
			row["channel_id"]: (row["after_message_id"], row["completed_at"])
			for row in self.conn.execute(
				"""
				SELECT channel_id, after_message_id, completed_at
				FROM history_imports
				WHERE guild_id = ?
				""",
				(guild_id,),
			)
		}

	def begin_history_scan(self, guild_id: int, channel_id: int):
		"""Mark a location incomplete until its current scan reaches Discord's end."""
		now = int(datetime.now(timezone.utc).timestamp())
		self.conn.execute(
			"""
			INSERT INTO history_imports (
				channel_id, guild_id, completed_at, updated_at
			) VALUES (?, ?, NULL, ?)
			ON CONFLICT(channel_id) DO UPDATE SET
				guild_id = excluded.guild_id,
				completed_at = NULL,
				updated_at = excluded.updated_at
			""",
			(channel_id, guild_id, now),
		)
		self.conn.commit()

	def update_history_cursor(
		self,
		guild_id: int,
		channel_id: int,
		after_message_id: int | None,
		*,
		complete: bool = False,
		migration_user_id: int | None = None,
		migration_timestamp: int | None = None,
	):
		now = int(datetime.now(timezone.utc).timestamp())
		self.conn.execute(
			"""
			INSERT INTO history_imports (
				channel_id, guild_id, after_message_id, migration_user_id,
				migration_timestamp, completed_at, updated_at
			) VALUES (?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(channel_id) DO UPDATE SET
				guild_id = excluded.guild_id,
				after_message_id = COALESCE(
					excluded.after_message_id, history_imports.after_message_id
				),
				migration_user_id = COALESCE(
					excluded.migration_user_id, history_imports.migration_user_id
				),
				migration_timestamp = COALESCE(
					excluded.migration_timestamp, history_imports.migration_timestamp
				),
				completed_at = CASE
					WHEN excluded.completed_at IS NOT NULL THEN excluded.completed_at
					ELSE history_imports.completed_at
				END,
				updated_at = excluded.updated_at
			""",
			(
				channel_id,
				guild_id,
				after_message_id,
				migration_user_id,
				migration_timestamp,
				now if complete else None,
				now,
			),
		)
		self.conn.commit()

	@staticmethod
	def _where(
		guild_id: int,
		cutoff: int | None,
		*,
		channel_id: int | None = None,
		parent_channel_id: int | None = None,
		user_id: int | None = None,
	) -> tuple[str, list[int]]:
		where = ["m.guild_id = ?"]
		params = [guild_id]
		for column, value in (
			("m.channel_id", channel_id),
			("m.parent_channel_id", parent_channel_id),
			("m.user_id", user_id),
		):
			if value is not None:
				where.append(f"{column} = ?")
				params.append(value)
		if cutoff is not None:
			where.append("m.created_at >= ?")
			params.append(cutoff)
		return " AND ".join(where), params

	def summary(
		self,
		guild_id: int,
		cutoff: int | None,
		*,
		channel_id: int | None = None,
		parent_channel_id: int | None = None,
		user_id: int | None = None,
	) -> dict:
		where, params = self._where(
			guild_id,
			cutoff,
			channel_id=channel_id,
			parent_channel_id=parent_channel_id,
			user_id=user_id,
		)
		row = self.conn.execute(
			f"""
			SELECT COUNT(*) AS messages,
				COUNT(DISTINCT m.user_id) AS members,
				COUNT(DISTINCT m.channel_id) AS channels,
				MIN(m.created_at) AS first_message_at,
				MAX(m.created_at) AS last_message_at
			FROM messages AS m
			WHERE {where}
			""",
			params,
		).fetchone()
		return dict(row)

	def top_users(
		self,
		guild_id: int,
		cutoff: int | None,
		*,
		channel_id: int | None = None,
		parent_channel_id: int | None = None,
		limit: int = 10,
	) -> list[dict]:
		where, params = self._where(
			guild_id,
			cutoff,
			channel_id=channel_id,
			parent_channel_id=parent_channel_id,
		)
		rows = self.conn.execute(
			f"""
			SELECT m.user_id, COUNT(*) AS messages
			FROM messages AS m
			WHERE {where}
			GROUP BY m.user_id
			ORDER BY messages DESC, m.user_id
			LIMIT ?
			""",
			[*params, limit],
		).fetchall()
		return [dict(row) for row in rows]

	def top_channels(
		self,
		guild_id: int,
		cutoff: int | None,
		*,
		channel_id: int | None = None,
		parent_channel_id: int | None = None,
		user_id: int | None = None,
		limit: int = 10,
	) -> list[dict]:
		where, params = self._where(
			guild_id,
			cutoff,
			channel_id=channel_id,
			parent_channel_id=parent_channel_id,
			user_id=user_id,
		)
		rows = self.conn.execute(
			f"""
			SELECT m.channel_id, c.name, c.kind, c.parent_channel_id,
				COUNT(*) AS messages
			FROM messages AS m
			JOIN channels AS c ON c.channel_id = m.channel_id
			WHERE {where}
			GROUP BY m.channel_id, c.name, c.kind, c.parent_channel_id
			ORDER BY messages DESC, m.channel_id
			LIMIT ?
			""",
			[*params, limit],
		).fetchall()
		return [dict(row) for row in rows]

	def member_rank(
		self,
		guild_id: int,
		user_id: int,
		cutoff: int | None,
		*,
		channel_id: int | None = None,
		parent_channel_id: int | None = None,
	) -> int | None:
		where, params = self._where(
			guild_id,
			cutoff,
			channel_id=channel_id,
			parent_channel_id=parent_channel_id,
		)
		rows = self.conn.execute(
			f"""
			SELECT m.user_id, COUNT(*) AS messages
			FROM messages AS m
			WHERE {where}
			GROUP BY m.user_id
			ORDER BY messages DESC, m.user_id
			""",
			params,
		).fetchall()
		for rank, row in enumerate(rows, start=1):
			if row["user_id"] == user_id:
				return rank
		return None
