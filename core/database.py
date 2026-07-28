import sqlite3
import time
import re
from pathlib import Path
from typing import Optional

from core.defaults import (
	DEFAULT_DETECTION_THRESHOLD,
	DEFAULT_OCR_SINGLE_IMAGE_LOOKBACK_DAYS,
	DEFAULT_OCR_SINGLE_IMAGE_MAX_MESSAGES,
	DEFAULT_OCR_KEYWORDS,
	DEFAULT_PRESSURE_BANNED_WORDS,
	DEFAULT_PRESSURE_SETTINGS,
)

DB_PATH = Path("attachmentbot.sqlite3")

FEATURE_OCR_GIVE_ROLE = "ocr_give_role"
FEATURE_OCR_DELETE_MESSAGE = "ocr_delete_message"

class BotDatabase:
	def __init__(self, path: Path = DB_PATH):
		self.path = path
		self.conn = sqlite3.connect(self.path)
		self.conn.row_factory = sqlite3.Row
		self.conn.execute("PRAGMA foreign_keys = ON")
		self.setup()
		self.migrate_pressure_scale()

	def setup(self):
		self.conn.executescript(
			"""
			CREATE TABLE IF NOT EXISTS guild_settings (
				guild_id INTEGER PRIMARY KEY,
				enabled INTEGER NOT NULL DEFAULT 0,
				log_channel_id INTEGER,
				role_id INTEGER,
				detection_threshold INTEGER NOT NULL DEFAULT 10,
				created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
				updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
			);

			CREATE TABLE IF NOT EXISTS manager_roles (
				guild_id INTEGER NOT NULL,
				role_id INTEGER NOT NULL,
				PRIMARY KEY (guild_id, role_id),
				FOREIGN KEY (guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
			);

			CREATE TABLE IF NOT EXISTS channel_blacklist (
				guild_id INTEGER NOT NULL,
				channel_id INTEGER NOT NULL,
				PRIMARY KEY (guild_id, channel_id),
				FOREIGN KEY (guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
			);

			CREATE TABLE IF NOT EXISTS guild_keywords (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				guild_id INTEGER NOT NULL,
				keyword TEXT NOT NULL,
				points INTEGER NOT NULL,
				UNIQUE (guild_id, keyword),
				FOREIGN KEY (guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
			);

			CREATE TABLE IF NOT EXISTS keyword_aliases (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				keyword_id INTEGER NOT NULL,
				alias TEXT NOT NULL,
				UNIQUE (keyword_id, alias),
				FOREIGN KEY (keyword_id) REFERENCES guild_keywords(id) ON DELETE CASCADE
			);

			CREATE TABLE IF NOT EXISTS guild_feature_flags (
				guild_id INTEGER NOT NULL,
				feature TEXT NOT NULL,
				enabled INTEGER NOT NULL DEFAULT 0,
				PRIMARY KEY (guild_id, feature),
				FOREIGN KEY (guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
			);

			CREATE TABLE IF NOT EXISTS schema_metadata (
				key TEXT PRIMARY KEY,
				value TEXT NOT NULL
			);

			CREATE TABLE IF NOT EXISTS pressure_settings (
				guild_id INTEGER PRIMARY KEY,
				enabled INTEGER NOT NULL DEFAULT 0,
				log_channel_id INTEGER,
				threshold INTEGER NOT NULL DEFAULT 100,
				decay_per_second REAL NOT NULL DEFAULT 3.3,
				base_pressure INTEGER NOT NULL DEFAULT 10,
				attachment_pressure INTEGER NOT NULL DEFAULT 15,
				embed_pressure INTEGER NOT NULL DEFAULT 15,
				mention_pressure INTEGER NOT NULL DEFAULT 10,
				link_pressure INTEGER NOT NULL DEFAULT 15,
				duplicate_pressure INTEGER NOT NULL DEFAULT 35,
				new_member_pressure INTEGER NOT NULL DEFAULT 20,
				line_pressure INTEGER NOT NULL DEFAULT 5,
				solo_emote_pressure INTEGER NOT NULL DEFAULT 30,
				gif_pressure INTEGER NOT NULL DEFAULT 50,
				banned_word_pressure INTEGER NOT NULL DEFAULT 500,
				new_member_hours INTEGER NOT NULL DEFAULT 24,
				delete_message INTEGER NOT NULL DEFAULT 0,
				give_role INTEGER NOT NULL DEFAULT 0,
				updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
				FOREIGN KEY (guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
			);

			CREATE TABLE IF NOT EXISTS pressure_channel_thresholds (
				guild_id INTEGER NOT NULL,
				channel_id INTEGER NOT NULL,
				threshold INTEGER NOT NULL,
				PRIMARY KEY (guild_id, channel_id),
				FOREIGN KEY (guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
			);

			CREATE TABLE IF NOT EXISTS pressure_banned_words (
				guild_id INTEGER NOT NULL,
				word TEXT NOT NULL,
				PRIMARY KEY (guild_id, word),
				FOREIGN KEY (guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
			);

			CREATE TABLE IF NOT EXISTS temporary_roles (
				guild_id INTEGER NOT NULL,
				user_id INTEGER NOT NULL,
				role_id INTEGER NOT NULL,
				expires_at INTEGER NOT NULL,
				reason TEXT NOT NULL DEFAULT '',
				created_at INTEGER NOT NULL,
				PRIMARY KEY (guild_id, user_id, role_id)
			);

			CREATE TABLE IF NOT EXISTS territory_settings (
				guild_id INTEGER PRIMARY KEY,
				enabled INTEGER NOT NULL DEFAULT 0,
				update_interval_minutes INTEGER NOT NULL DEFAULT 60,
				updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
				FOREIGN KEY (guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
			);

			CREATE TABLE IF NOT EXISTS territory_channel_blacklist (
				guild_id INTEGER NOT NULL,
				channel_id INTEGER NOT NULL,
				PRIMARY KEY (guild_id, channel_id),
				FOREIGN KEY (guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
			);

			CREATE TABLE IF NOT EXISTS territory_channel_state (
				guild_id INTEGER NOT NULL,
				channel_id INTEGER NOT NULL,
				last_managed_line TEXT NOT NULL,
				line_created INTEGER NOT NULL DEFAULT 0,
				PRIMARY KEY (guild_id, channel_id),
				FOREIGN KEY (guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
			);

			CREATE TABLE IF NOT EXISTS recycle_settings (
				guild_id INTEGER PRIMARY KEY,
				enabled INTEGER NOT NULL DEFAULT 0,
				channel_id INTEGER,
				checkpoint_message_id INTEGER,
				reply_fallback INTEGER NOT NULL DEFAULT 0,
				ignore_replies INTEGER NOT NULL DEFAULT 0,
				history_days INTEGER NOT NULL DEFAULT 30,
				updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
				FOREIGN KEY (guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
			);

			CREATE TABLE IF NOT EXISTS recycle_url_signatures (
				guild_id INTEGER NOT NULL,
				channel_id INTEGER NOT NULL,
				signature TEXT NOT NULL,
				first_message_id INTEGER NOT NULL,
				PRIMARY KEY (guild_id, channel_id, signature)
			);

			CREATE TABLE IF NOT EXISTS recycle_image_hashes (
				guild_id INTEGER NOT NULL,
				channel_id INTEGER NOT NULL,
				phash TEXT NOT NULL,
				first_message_id INTEGER NOT NULL,
				PRIMARY KEY (guild_id, channel_id, phash)
			);

			CREATE TABLE IF NOT EXISTS recycle_image_hash_buckets (
				guild_id INTEGER NOT NULL,
				channel_id INTEGER NOT NULL,
				bucket_index INTEGER NOT NULL,
				bucket_value TEXT NOT NULL,
				phash TEXT NOT NULL,
				PRIMARY KEY (guild_id, channel_id, bucket_index, bucket_value, phash)
			);

			CREATE INDEX IF NOT EXISTS recycle_image_hash_bucket_lookup
			ON recycle_image_hash_buckets (
				guild_id, channel_id, bucket_index, bucket_value
			);

			CREATE TABLE IF NOT EXISTS recycle_processed_messages (
				guild_id INTEGER NOT NULL,
				channel_id INTEGER NOT NULL,
				message_id INTEGER NOT NULL,
				PRIMARY KEY (guild_id, channel_id, message_id)
			);

			CREATE TABLE IF NOT EXISTS relay_settings (
				guild_id INTEGER PRIMARY KEY,
				enabled INTEGER NOT NULL DEFAULT 0,
				updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
				FOREIGN KEY (guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
			);

			CREATE TABLE IF NOT EXISTS relay_routes (
				route_id INTEGER PRIMARY KEY AUTOINCREMENT,
				guild_id INTEGER NOT NULL,
				source_guild_id INTEGER NOT NULL,
				source_channel_id INTEGER NOT NULL,
				target_thread_id INTEGER NOT NULL,
				created_by INTEGER NOT NULL,
				created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
				UNIQUE (guild_id, source_channel_id, target_thread_id),
				FOREIGN KEY (guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
			);

			CREATE INDEX IF NOT EXISTS relay_route_source_lookup
			ON relay_routes (source_channel_id);

			CREATE TABLE IF NOT EXISTS relay_deliveries (
				route_id INTEGER NOT NULL,
				source_message_id INTEGER NOT NULL,
				target_message_id INTEGER NOT NULL,
				delivered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
				PRIMARY KEY (route_id, source_message_id),
				FOREIGN KEY (route_id) REFERENCES relay_routes(route_id) ON DELETE CASCADE
			);
			"""
		)
		self.add_missing_guild_columns()
		self.add_missing_pressure_columns()
		self.add_missing_territory_columns()
		self.add_missing_recycle_columns()
		self.conn.commit()

	def add_missing_guild_columns(self):
		rows = self.conn.execute("PRAGMA table_info(guild_settings)").fetchall()
		existing = {row["name"] for row in rows}
		columns = {
			"single_image_max_messages": "INTEGER NOT NULL DEFAULT 0",
			"single_image_lookback_days": "INTEGER NOT NULL DEFAULT 7",
		}
		for column, definition in columns.items():
			if column not in existing:
				self.conn.execute(f"ALTER TABLE guild_settings ADD COLUMN {column} {definition}")

	def add_missing_pressure_columns(self):
		rows = self.conn.execute("PRAGMA table_info(pressure_settings)").fetchall()
		existing = {row["name"] for row in rows}
		columns = {
			"log_channel_id": "INTEGER",
			"line_pressure": "INTEGER NOT NULL DEFAULT 5",
			"solo_emote_pressure": "INTEGER NOT NULL DEFAULT 30",
			"gif_pressure": "INTEGER NOT NULL DEFAULT 50",
			"banned_word_pressure": "INTEGER NOT NULL DEFAULT 500",
			"role_duration_seconds": "INTEGER NOT NULL DEFAULT 3600",
		}
		for column, definition in columns.items():
			if column not in existing:
				self.conn.execute(f"ALTER TABLE pressure_settings ADD COLUMN {column} {definition}")

	def add_missing_territory_columns(self):
		rows = self.conn.execute("PRAGMA table_info(territory_settings)").fetchall()
		existing = {row["name"] for row in rows}
		if "enabled" not in existing:
			self.conn.execute(
				"ALTER TABLE territory_settings ADD COLUMN enabled INTEGER NOT NULL DEFAULT 0"
			)

	def add_missing_recycle_columns(self):
		rows = self.conn.execute("PRAGMA table_info(recycle_settings)").fetchall()
		existing = {row["name"] for row in rows}
		columns = {
			"reply_fallback": "INTEGER NOT NULL DEFAULT 0",
			"ignore_replies": "INTEGER NOT NULL DEFAULT 0",
			"history_days": "INTEGER NOT NULL DEFAULT 30",
		}
		for column, definition in columns.items():
			if column not in existing:
				self.conn.execute(
					f"ALTER TABLE recycle_settings ADD COLUMN {column} {definition}"
				)
		if "history_days" not in existing:
			for table in (
				"recycle_url_signatures",
				"recycle_image_hashes",
				"recycle_image_hash_buckets",
				"recycle_processed_messages",
			):
				self.conn.execute(f"DELETE FROM {table}")
			self.conn.execute("UPDATE recycle_settings SET checkpoint_message_id = NULL")

	def get_metadata(self, key: str) -> Optional[str]:
		row = self.conn.execute(
			"SELECT value FROM schema_metadata WHERE key = ?",
			(key,),
		).fetchone()
		return row["value"] if row else None

	def set_metadata(self, key: str, value: str):
		self.conn.execute(
			"""
			INSERT INTO schema_metadata (key, value)
			VALUES (?, ?)
			ON CONFLICT(key) DO UPDATE SET value = excluded.value
			""",
			(key, value),
		)
		self.conn.commit()

	def migrate_pressure_scale(self):
		if self.get_metadata("pressure_scale") == "whole_v1":
			self.migrate_pressure_defaults()
			return

		pressure_columns = [
			"threshold",
			"base_pressure",
			"attachment_pressure",
			"embed_pressure",
			"mention_pressure",
			"link_pressure",
			"duplicate_pressure",
			"new_member_pressure",
		]
		for column in pressure_columns:
			self.conn.execute(
				f"UPDATE pressure_settings SET {column} = CAST(ROUND({column} * 10) AS INTEGER)"
			)
		self.conn.execute(
			"UPDATE pressure_channel_thresholds SET threshold = CAST(ROUND(threshold * 10) AS INTEGER)"
		)
		self.set_metadata("pressure_scale", "whole_v1")
		self.migrate_pressure_defaults()

	def migrate_pressure_defaults(self):
		if self.get_metadata("pressure_defaults") == "spec_v2":
			return

		self.conn.execute(
			"""
			UPDATE pressure_settings
			SET
				threshold = 100,
				decay_per_second = 3.3,
				base_pressure = 10,
				attachment_pressure = 15,
				embed_pressure = 15,
				mention_pressure = 10,
				link_pressure = 15,
				duplicate_pressure = 35,
				line_pressure = 5,
				solo_emote_pressure = 30,
				gif_pressure = 50,
				banned_word_pressure = 500,
				role_duration_seconds = 3600
			"""
		)
		self.set_metadata("pressure_defaults", "spec_v2")

	def ensure_guild(self, guild_id: int, seed_defaults: bool = True):
		result = self.conn.execute(
			"INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)",
			(guild_id,),
		)
		created = result.rowcount > 0
		self.conn.execute(
			"INSERT OR IGNORE INTO pressure_settings (guild_id) VALUES (?)",
			(guild_id,),
		)
		self.conn.execute(
			"INSERT OR IGNORE INTO territory_settings (guild_id) VALUES (?)",
			(guild_id,),
		)
		self.conn.execute(
			"INSERT OR IGNORE INTO recycle_settings (guild_id) VALUES (?)",
			(guild_id,),
		)
		self.conn.execute(
			"INSERT OR IGNORE INTO relay_settings (guild_id) VALUES (?)",
			(guild_id,),
		)
		if created and seed_defaults:
			self.seed_default_guild(guild_id)
		self.conn.commit()

	def seed_default_guild(self, guild_id: int):
		self.conn.execute(
			"""
			UPDATE guild_settings
			SET enabled = 0,
				log_channel_id = NULL,
				role_id = NULL,
				detection_threshold = ?,
				single_image_max_messages = ?,
				single_image_lookback_days = ?,
				updated_at = CURRENT_TIMESTAMP
			WHERE guild_id = ?
			""",
			(
				DEFAULT_DETECTION_THRESHOLD,
				DEFAULT_OCR_SINGLE_IMAGE_MAX_MESSAGES,
				DEFAULT_OCR_SINGLE_IMAGE_LOOKBACK_DAYS,
				guild_id,
			),
		)

		pressure_updates = {
			key: int(bool(value)) if key in {"enabled", "delete_message", "give_role"} else value
			for key, value in DEFAULT_PRESSURE_SETTINGS.items()
		}
		assignments = ", ".join(f"{key} = ?" for key in pressure_updates)
		self.conn.execute(
			f"UPDATE pressure_settings SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE guild_id = ?",
			[*pressure_updates.values(), guild_id],
		)

		for keyword, data in DEFAULT_OCR_KEYWORDS.items():
			self.insert_keyword(guild_id, keyword, data["points"], data.get("aliases", []))

		for word in DEFAULT_PRESSURE_BANNED_WORDS:
			self.conn.execute(
				"INSERT OR IGNORE INTO pressure_banned_words (guild_id, word) VALUES (?, ?)",
				(guild_id, word),
			)

	def update_guild_settings(self, guild_id: int, **values):
		self.ensure_guild(guild_id)
		allowed = {
			"enabled",
			"log_channel_id",
			"role_id",
			"detection_threshold",
			"single_image_max_messages",
			"single_image_lookback_days",
		}
		updates = {key: value for key, value in values.items() if key in allowed}
		if not updates:
			return

		for key in ("enabled",):
			if key in updates:
				updates[key] = int(bool(updates[key]))

		assignments = ", ".join(f"{key} = ?" for key in updates)
		params = list(updates.values()) + [guild_id]
		self.conn.execute(
			f"UPDATE guild_settings SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE guild_id = ?",
			params,
		)
		self.conn.commit()

	def get_guild_settings(self, guild_id: int) -> Optional[dict]:
		row = self.conn.execute(
			"SELECT * FROM guild_settings WHERE guild_id = ?",
			(guild_id,),
		).fetchone()
		if not row:
			return None

		return {
			"guild_id": row["guild_id"],
			"enabled": bool(row["enabled"]),
			"log_channel_id": row["log_channel_id"],
			"role_id": row["role_id"],
			"detection_threshold": row["detection_threshold"],
			"single_image_max_messages": row["single_image_max_messages"],
			"single_image_lookback_days": row["single_image_lookback_days"],
			"channel_blacklist": self.get_blacklisted_channels(guild_id),
			"keywords": self.get_keywords(guild_id),
			"features": self.get_feature_flags(guild_id),
			"manager_roles": self.get_manager_roles(guild_id),
			"pressure": self.get_pressure_settings(guild_id),
			"territory": self.get_territory_settings(guild_id),
			"recycle": self.get_recycle_settings(guild_id),
			"relay": self.get_relay_settings(guild_id),
		}

	def get_shared_settings(self, guild_id: int) -> Optional[dict]:
		row = self.conn.execute(
			"SELECT guild_id, log_channel_id, role_id FROM guild_settings WHERE guild_id = ?",
			(guild_id,),
		).fetchone()
		if not row:
			return None
		return dict(row)

	def get_ocr_settings(self, guild_id: int) -> Optional[dict]:
		row = self.conn.execute(
			"""
			SELECT guild_id, enabled, log_channel_id, role_id, detection_threshold,
			       single_image_max_messages, single_image_lookback_days
			FROM guild_settings WHERE guild_id = ?
			""",
			(guild_id,),
		).fetchone()
		if not row:
			return None
		return {
			**dict(row),
			"enabled": bool(row["enabled"]),
			"channel_blacklist": self.get_blacklisted_channels(guild_id),
			"keywords": self.get_keywords(guild_id),
			"features": self.get_feature_flags(guild_id),
		}

	def get_recycle_settings(self, guild_id: int) -> dict:
		self.ensure_guild(guild_id)
		row = self.conn.execute(
			"""
			SELECT enabled, channel_id, checkpoint_message_id, reply_fallback,
				ignore_replies, history_days
			FROM recycle_settings WHERE guild_id = ?
			""",
			(guild_id,),
		).fetchone()
		return {
			"enabled": bool(row["enabled"]),
			"channel_id": row["channel_id"],
			"checkpoint_message_id": row["checkpoint_message_id"],
			"reply_fallback": bool(row["reply_fallback"]),
			"ignore_replies": bool(row["ignore_replies"]),
			"history_days": row["history_days"],
		}

	def set_recycle_enabled(self, guild_id: int, enabled: bool):
		self.ensure_guild(guild_id)
		self.conn.execute(
			"""
			UPDATE recycle_settings
			SET enabled = ?, updated_at = CURRENT_TIMESTAMP
			WHERE guild_id = ?
			""",
			(int(bool(enabled)), guild_id),
		)
		self.conn.commit()

	def set_recycle_reply_fallback(self, guild_id: int, enabled: bool):
		self.ensure_guild(guild_id)
		self.conn.execute(
			"""
			UPDATE recycle_settings
			SET reply_fallback = ?, updated_at = CURRENT_TIMESTAMP
			WHERE guild_id = ?
			""",
			(int(bool(enabled)), guild_id),
		)
		self.conn.commit()

	def set_recycle_ignore_replies(self, guild_id: int, enabled: bool):
		self.ensure_guild(guild_id)
		self.conn.execute(
			"""
			UPDATE recycle_settings
			SET ignore_replies = ?, updated_at = CURRENT_TIMESTAMP
			WHERE guild_id = ?
			""",
			(int(bool(enabled)), guild_id),
		)
		self.conn.commit()

	def clear_recycle_index(self, guild_id: int):
		for table in (
			"recycle_url_signatures",
			"recycle_image_hashes",
			"recycle_image_hash_buckets",
			"recycle_processed_messages",
		):
			self.conn.execute(f"DELETE FROM {table} WHERE guild_id = ?", (guild_id,))

	def set_recycle_history_days(self, guild_id: int, days: int) -> bool:
		if days < 1:
			raise ValueError("Recycle history must be at least one day.")
		self.ensure_guild(guild_id)
		if self.get_recycle_settings(guild_id)["history_days"] == days:
			return False
		self.clear_recycle_index(guild_id)
		self.conn.execute(
			"""
			UPDATE recycle_settings
			SET history_days = ?, checkpoint_message_id = NULL,
				updated_at = CURRENT_TIMESTAMP
			WHERE guild_id = ?
			""",
			(days, guild_id),
		)
		self.conn.commit()
		return True

	def set_recycle_channel(self, guild_id: int, channel_id: int):
		self.ensure_guild(guild_id)
		current = self.get_recycle_settings(guild_id)
		if current["channel_id"] == channel_id:
			return
		self.clear_recycle_index(guild_id)
		self.conn.execute(
			"""
			UPDATE recycle_settings
			SET channel_id = ?, checkpoint_message_id = NULL,
				updated_at = CURRENT_TIMESTAMP
			WHERE guild_id = ?
			""",
			(channel_id, guild_id),
		)
		self.conn.commit()

	def update_recycle_checkpoint(self, guild_id: int, message_id: int):
		self.ensure_guild(guild_id)
		self.conn.execute(
			"""
			UPDATE recycle_settings
			SET checkpoint_message_id = CASE
					WHEN checkpoint_message_id IS NULL OR checkpoint_message_id < ? THEN ?
					ELSE checkpoint_message_id
				END,
				updated_at = CURRENT_TIMESTAMP
			WHERE guild_id = ?
			""",
			(message_id, message_id, guild_id),
		)
		self.conn.commit()

	def get_relay_settings(self, guild_id: int) -> dict:
		self.ensure_guild(guild_id)
		row = self.conn.execute(
			"SELECT enabled FROM relay_settings WHERE guild_id = ?",
			(guild_id,),
		).fetchone()
		return {
			"enabled": bool(row["enabled"]),
			"routes": self.get_relay_routes(guild_id),
		}

	def set_relay_enabled(self, guild_id: int, enabled: bool):
		self.ensure_guild(guild_id)
		self.conn.execute(
			"""
			UPDATE relay_settings
			SET enabled = ?, updated_at = CURRENT_TIMESTAMP
			WHERE guild_id = ?
			""",
			(int(bool(enabled)), guild_id),
		)
		self.conn.commit()

	def get_relay_routes(self, guild_id: int) -> list[dict]:
		rows = self.conn.execute(
			"""
			SELECT route_id, guild_id, source_guild_id, source_channel_id,
				target_thread_id, created_by, created_at
			FROM relay_routes
			WHERE guild_id = ?
			ORDER BY route_id
			""",
			(guild_id,),
		).fetchall()
		return [dict(row) for row in rows]

	def get_active_relay_routes(self, source_channel_id: int) -> list[dict]:
		rows = self.conn.execute(
			"""
			SELECT r.route_id, r.guild_id, r.source_guild_id, r.source_channel_id,
				r.target_thread_id, r.created_by, r.created_at
			FROM relay_routes r
			JOIN relay_settings s ON s.guild_id = r.guild_id
			WHERE r.source_channel_id = ? AND s.enabled = 1
			ORDER BY r.route_id
			""",
			(source_channel_id,),
		).fetchall()
		return [dict(row) for row in rows]

	def add_relay_route(
		self,
		guild_id: int,
		source_guild_id: int,
		source_channel_id: int,
		target_thread_id: int,
		created_by: int,
	) -> int:
		self.ensure_guild(guild_id)
		try:
			result = self.conn.execute(
				"""
				INSERT INTO relay_routes (
					guild_id, source_guild_id, source_channel_id,
					target_thread_id, created_by
				) VALUES (?, ?, ?, ?, ?)
				""",
				(
					guild_id, source_guild_id, source_channel_id,
					target_thread_id, created_by,
				),
			)
		except sqlite3.IntegrityError as exc:
			raise ValueError("That relay route already exists.") from exc
		self.conn.commit()
		return result.lastrowid

	def remove_relay_route(self, guild_id: int, route_id: int) -> bool:
		result = self.conn.execute(
			"DELETE FROM relay_routes WHERE guild_id = ? AND route_id = ?",
			(guild_id, route_id),
		)
		self.conn.commit()
		return result.rowcount > 0

	def relay_delivery_exists(self, route_id: int, source_message_id: int) -> bool:
		row = self.conn.execute(
			"""
			SELECT 1 FROM relay_deliveries
			WHERE route_id = ? AND source_message_id = ?
			""",
			(route_id, source_message_id),
		).fetchone()
		return row is not None

	def mark_relay_delivery(
		self,
		route_id: int,
		source_message_id: int,
		target_message_id: int,
	):
		self.conn.execute(
			"""
			INSERT OR IGNORE INTO relay_deliveries (
				route_id, source_message_id, target_message_id
			) VALUES (?, ?, ?)
			""",
			(route_id, source_message_id, target_message_id),
		)
		self.conn.commit()

	def get_territory_settings(self, guild_id: int) -> dict:
		self.ensure_guild(guild_id)
		row = self.conn.execute(
			"SELECT enabled, update_interval_minutes FROM territory_settings WHERE guild_id = ?",
			(guild_id,),
		).fetchone()
		return {
			"enabled": bool(row["enabled"]),
			"update_interval_minutes": row["update_interval_minutes"],
			"channel_blacklist": self.get_territory_blacklist(guild_id),
		}

	def set_territory_enabled(self, guild_id: int, enabled: bool):
		self.ensure_guild(guild_id)
		self.conn.execute(
			"""
			UPDATE territory_settings
			SET enabled = ?, updated_at = CURRENT_TIMESTAMP
			WHERE guild_id = ?
			""",
			(int(bool(enabled)), guild_id),
		)
		self.conn.commit()

	def set_territory_update_interval(self, guild_id: int, minutes: int):
		if minutes < 1:
			raise ValueError("Territory update interval must be at least one minute.")
		self.ensure_guild(guild_id)
		self.conn.execute(
			"""
			UPDATE territory_settings
			SET update_interval_minutes = ?, updated_at = CURRENT_TIMESTAMP
			WHERE guild_id = ?
			""",
			(minutes, guild_id),
		)
		self.conn.commit()

	def get_territory_blacklist(self, guild_id: int) -> set[int]:
		rows = self.conn.execute(
			"SELECT channel_id FROM territory_channel_blacklist WHERE guild_id = ?",
			(guild_id,),
		).fetchall()
		return {row["channel_id"] for row in rows}

	def add_territory_blacklist(self, guild_id: int, channel_id: int):
		self.ensure_guild(guild_id)
		self.conn.execute(
			"INSERT OR IGNORE INTO territory_channel_blacklist (guild_id, channel_id) VALUES (?, ?)",
			(guild_id, channel_id),
		)
		self.conn.commit()

	def remove_territory_blacklist(self, guild_id: int, channel_id: int):
		self.conn.execute(
			"DELETE FROM territory_channel_blacklist WHERE guild_id = ? AND channel_id = ?",
			(guild_id, channel_id),
		)
		self.conn.commit()

	def get_territory_channel_state(self, guild_id: int, channel_id: int) -> Optional[dict]:
		row = self.conn.execute(
			"""
			SELECT last_managed_line, line_created
			FROM territory_channel_state
			WHERE guild_id = ? AND channel_id = ?
			""",
			(guild_id, channel_id),
		).fetchone()
		if not row:
			return None
		return {
			"last_managed_line": row["last_managed_line"],
			"line_created": bool(row["line_created"]),
		}

	def get_territory_channel_states(self, guild_id: int) -> dict[int, dict]:
		rows = self.conn.execute(
			"""
			SELECT channel_id, last_managed_line, line_created
			FROM territory_channel_state WHERE guild_id = ?
			""",
			(guild_id,),
		).fetchall()
		return {
			row["channel_id"]: {
				"last_managed_line": row["last_managed_line"],
				"line_created": bool(row["line_created"]),
			}
			for row in rows
		}

	def set_territory_channel_state(
		self,
		guild_id: int,
		channel_id: int,
		last_managed_line: str,
		line_created: bool,
	):
		self.ensure_guild(guild_id)
		self.conn.execute(
			"""
			INSERT INTO territory_channel_state (
				guild_id, channel_id, last_managed_line, line_created
			) VALUES (?, ?, ?, ?)
			ON CONFLICT(guild_id, channel_id) DO UPDATE SET
				last_managed_line = excluded.last_managed_line,
				line_created = excluded.line_created
			""",
			(guild_id, channel_id, last_managed_line, int(bool(line_created))),
		)
		self.conn.commit()

	def remove_territory_channel_state(self, guild_id: int, channel_id: int):
		self.conn.execute(
			"DELETE FROM territory_channel_state WHERE guild_id = ? AND channel_id = ?",
			(guild_id, channel_id),
		)
		self.conn.commit()

	def set_pressure_channel_threshold(self, guild_id: int, channel_id: int, threshold: int):
		self.ensure_guild(guild_id)
		self.conn.execute(
			"""
			INSERT INTO pressure_channel_thresholds (guild_id, channel_id, threshold)
			VALUES (?, ?, ?)
			ON CONFLICT(guild_id, channel_id) DO UPDATE SET threshold = excluded.threshold
			""",
			(guild_id, channel_id, threshold),
		)
		self.conn.commit()

	def remove_pressure_channel_threshold(self, guild_id: int, channel_id: int):
		self.conn.execute(
			"DELETE FROM pressure_channel_thresholds WHERE guild_id = ? AND channel_id = ?",
			(guild_id, channel_id),
		)
		self.conn.commit()

	def get_pressure_channel_threshold(self, guild_id: int, channel_id: int) -> Optional[int]:
		row = self.conn.execute(
			"SELECT threshold FROM pressure_channel_thresholds WHERE guild_id = ? AND channel_id = ?",
			(guild_id, channel_id),
		).fetchone()
		return row["threshold"] if row else None

	def get_pressure_channel_thresholds(self, guild_id: int) -> dict[int, int]:
		rows = self.conn.execute(
			"SELECT channel_id, threshold FROM pressure_channel_thresholds WHERE guild_id = ? ORDER BY channel_id",
			(guild_id,),
		).fetchall()
		return {row["channel_id"]: row["threshold"] for row in rows}

	def get_pressure_settings(self, guild_id: int) -> dict:
		self.ensure_guild(guild_id)
		row = self.conn.execute(
			"SELECT * FROM pressure_settings WHERE guild_id = ?",
			(guild_id,),
		).fetchone()
		if not row:
			return DEFAULT_PRESSURE_SETTINGS.copy()

		settings = dict(DEFAULT_PRESSURE_SETTINGS)
		for key in settings:
			value = row[key]
			if key in {"enabled", "delete_message", "give_role"}:
				settings[key] = bool(value)
			elif key == "log_channel_id":
				settings[key] = value
			elif key == "decay_per_second":
				settings[key] = float(value)
			elif key in {"new_member_hours", "role_duration_seconds"}:
				settings[key] = int(value)
			else:
				settings[key] = int(round(value))
		settings["channel_thresholds"] = self.get_pressure_channel_thresholds(guild_id)
		settings["banned_words"] = self.get_pressure_banned_words(guild_id)
		return settings

	def update_pressure_settings(self, guild_id: int, **values):
		self.ensure_guild(guild_id)
		allowed = set(DEFAULT_PRESSURE_SETTINGS)
		updates = {key: value for key, value in values.items() if key in allowed}
		if not updates:
			return

		for key in ("enabled", "delete_message", "give_role"):
			if key in updates:
				updates[key] = int(bool(updates[key]))
		for key in updates:
			if key == "log_channel_id":
				continue
			if key == "decay_per_second":
				updates[key] = float(updates[key])
			elif key not in {"enabled", "delete_message", "give_role"}:
				updates[key] = int(updates[key])

		assignments = ", ".join(f"{key} = ?" for key in updates)
		params = list(updates.values()) + [guild_id]
		self.conn.execute(
			f"UPDATE pressure_settings SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE guild_id = ?",
			params,
		)
		self.conn.commit()

	def add_temporary_role(self, guild_id: int, user_id: int, role_id: int, expires_at: int, reason: str):
		created_at = int(time.time())
		self.conn.execute(
			"""
			INSERT INTO temporary_roles (guild_id, user_id, role_id, expires_at, reason, created_at)
			VALUES (?, ?, ?, ?, ?, ?)
			ON CONFLICT(guild_id, user_id, role_id)
			DO UPDATE SET expires_at = excluded.expires_at, reason = excluded.reason
			""",
			(guild_id, user_id, role_id, expires_at, reason, created_at),
		)
		self.conn.commit()

	def remove_temporary_role(self, guild_id: int, user_id: int, role_id: int):
		self.conn.execute(
			"DELETE FROM temporary_roles WHERE guild_id = ? AND user_id = ? AND role_id = ?",
			(guild_id, user_id, role_id),
		)
		self.conn.commit()

	def get_due_temporary_roles(self, now: int) -> list[dict]:
		rows = self.conn.execute(
			"SELECT guild_id, user_id, role_id, expires_at, reason FROM temporary_roles WHERE expires_at <= ? ORDER BY expires_at",
			(now,),
		).fetchall()
		return [dict(row) for row in rows]

	def add_pressure_banned_word(self, guild_id: int, word: str):
		self.ensure_guild(guild_id)
		word = word.strip()
		if not word:
			raise ValueError("Banned word cannot be empty.")
		self.validate_pressure_banned_words([*self.get_pressure_banned_words(guild_id), word])
		self.conn.execute(
			"INSERT OR IGNORE INTO pressure_banned_words (guild_id, word) VALUES (?, ?)",
			(guild_id, word),
		)
		self.conn.commit()

	def set_pressure_banned_words(self, guild_id: int, words: list[str]):
		self.ensure_guild(guild_id)
		words = [word.strip() for word in words if word.strip()]
		self.validate_pressure_banned_words(words)

		self.conn.execute(
			"DELETE FROM pressure_banned_words WHERE guild_id = ?",
			(guild_id,),
		)
		self.conn.executemany(
			"INSERT OR IGNORE INTO pressure_banned_words (guild_id, word) VALUES (?, ?)",
			[(guild_id, word) for word in words],
		)
		self.conn.commit()

	def validate_pressure_banned_words(self, words: list[str]):
		if not words:
			return
		try:
			re.compile("(" + "|".join(words) + ")")
		except re.error as exc:
			raise ValueError(f"Banned word regex is invalid: {exc}") from exc

	def remove_pressure_banned_word(self, guild_id: int, word: str):
		self.conn.execute(
			"DELETE FROM pressure_banned_words WHERE guild_id = ? AND word = ?",
			(guild_id, word.strip()),
		)
		self.conn.commit()

	def get_pressure_banned_words(self, guild_id: int) -> list[str]:
		rows = self.conn.execute(
			"SELECT word FROM pressure_banned_words WHERE guild_id = ? ORDER BY word",
			(guild_id,),
		).fetchall()
		return [row["word"] for row in rows]

	def set_feature_enabled(self, guild_id: int, feature: str, enabled: bool):
		self.ensure_guild(guild_id)
		self.conn.execute(
			"""
			INSERT INTO guild_feature_flags (guild_id, feature, enabled)
			VALUES (?, ?, ?)
			ON CONFLICT(guild_id, feature) DO UPDATE SET enabled = excluded.enabled
			""",
			(guild_id, feature, int(enabled)),
		)
		self.conn.commit()

	def is_feature_enabled(self, guild_id: int, feature: str) -> bool:
		row = self.conn.execute(
			"SELECT enabled FROM guild_feature_flags WHERE guild_id = ? AND feature = ?",
			(guild_id, feature),
		).fetchone()
		return bool(row["enabled"]) if row else False

	def get_feature_flags(self, guild_id: int) -> dict:
		rows = self.conn.execute(
			"SELECT feature, enabled FROM guild_feature_flags WHERE guild_id = ?",
			(guild_id,),
		).fetchall()
		return {row["feature"]: bool(row["enabled"]) for row in rows}

	def add_manager_role(self, guild_id: int, role_id: int):
		self.ensure_guild(guild_id)
		self.conn.execute(
			"INSERT OR IGNORE INTO manager_roles (guild_id, role_id) VALUES (?, ?)",
			(guild_id, role_id),
		)
		self.conn.commit()

	def remove_manager_role(self, guild_id: int, role_id: int):
		self.conn.execute(
			"DELETE FROM manager_roles WHERE guild_id = ? AND role_id = ?",
			(guild_id, role_id),
		)
		self.conn.commit()

	def get_manager_roles(self, guild_id: int) -> set[int]:
		rows = self.conn.execute(
			"SELECT role_id FROM manager_roles WHERE guild_id = ?",
			(guild_id,),
		).fetchall()
		return {row["role_id"] for row in rows}

	def add_blacklisted_channel(self, guild_id: int, channel_id: int):
		self.ensure_guild(guild_id)
		self.conn.execute(
			"INSERT OR IGNORE INTO channel_blacklist (guild_id, channel_id) VALUES (?, ?)",
			(guild_id, channel_id),
		)
		self.conn.commit()

	def remove_blacklisted_channel(self, guild_id: int, channel_id: int):
		self.conn.execute(
			"DELETE FROM channel_blacklist WHERE guild_id = ? AND channel_id = ?",
			(guild_id, channel_id),
		)
		self.conn.commit()

	def get_blacklisted_channels(self, guild_id: int) -> set[int]:
		rows = self.conn.execute(
			"SELECT channel_id FROM channel_blacklist WHERE guild_id = ?",
			(guild_id,),
		).fetchall()
		return {row["channel_id"] for row in rows}

	def insert_keyword(self, guild_id: int, keyword: str, points: int, aliases=None):
		keyword = keyword.strip().lower()
		if not keyword:
			raise ValueError("Keyword cannot be empty.")

		self.conn.execute(
			"""
			INSERT INTO guild_keywords (guild_id, keyword, points)
			VALUES (?, ?, ?)
			ON CONFLICT(guild_id, keyword) DO UPDATE SET points = excluded.points
			""",
			(guild_id, keyword, points),
		)

		for alias in aliases or []:
			keyword_id = self.get_keyword_id(guild_id, keyword)
			alias = alias.strip().lower()
			if alias and keyword_id:
				self.conn.execute(
					"INSERT OR IGNORE INTO keyword_aliases (keyword_id, alias) VALUES (?, ?)",
					(keyword_id, alias),
				)

	def add_keyword(self, guild_id: int, keyword: str, points: int, aliases=None):
		self.ensure_guild(guild_id)
		self.insert_keyword(guild_id, keyword, points, aliases)
		self.conn.commit()

	def remove_keyword(self, guild_id: int, keyword: str):
		self.conn.execute(
			"DELETE FROM guild_keywords WHERE guild_id = ? AND keyword = ?",
			(guild_id, keyword.strip().lower()),
		)
		self.conn.commit()

	def add_alias(self, guild_id: int, keyword: str, alias: str):
		keyword_id = self.get_keyword_id(guild_id, keyword)
		if not keyword_id:
			raise ValueError("Keyword does not exist.")

		alias = alias.strip().lower()
		if not alias:
			raise ValueError("Alias cannot be empty.")

		self.conn.execute(
			"INSERT OR IGNORE INTO keyword_aliases (keyword_id, alias) VALUES (?, ?)",
			(keyword_id, alias),
		)
		self.conn.commit()

	def remove_alias(self, guild_id: int, keyword: str, alias: str):
		keyword_id = self.get_keyword_id(guild_id, keyword)
		if not keyword_id:
			return

		self.conn.execute(
			"DELETE FROM keyword_aliases WHERE keyword_id = ? AND alias = ?",
			(keyword_id, alias.strip().lower()),
		)
		self.conn.commit()

	def get_keyword_id(self, guild_id: int, keyword: str) -> Optional[int]:
		row = self.conn.execute(
			"SELECT id FROM guild_keywords WHERE guild_id = ? AND keyword = ?",
			(guild_id, keyword.strip().lower()),
		).fetchone()
		return row["id"] if row else None

	def get_keywords(self, guild_id: int) -> dict:
		rows = self.conn.execute(
			"SELECT id, keyword, points FROM guild_keywords WHERE guild_id = ?",
			(guild_id,),
		).fetchall()
		keywords = {}

		for row in rows:
			aliases = self.conn.execute(
				"SELECT alias FROM keyword_aliases WHERE keyword_id = ? ORDER BY alias",
				(row["id"],),
			).fetchall()
			keywords[row["keyword"]] = {
				"points": row["points"],
				"aliases": [alias["alias"] for alias in aliases],
			}

		return keywords


db = BotDatabase()
