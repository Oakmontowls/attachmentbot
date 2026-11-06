import hashlib
import hmac
import json
import mimetypes
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "static"
DEFAULT_DATABASE = ROOT / "message_stats.sqlite3"


def integer(value: str | None, default: int | None = None) -> int | None:
	if value is None or value == "":
		return default
	try:
		return int(value)
	except ValueError as exc:
		raise ValueError(f"Invalid integer: {value}") from exc


def json_safe_ids(value):
	"""Encode Discord snowflakes as strings so browsers cannot round them."""
	if isinstance(value, list):
		return [json_safe_ids(item) for item in value]
	if isinstance(value, dict):
		return {
			key: str(item) if key.endswith("_id") and item is not None else json_safe_ids(item)
			for key, item in value.items()
		}
	return value


class StatsRepository:
	def __init__(self, path: str | Path):
		self.path = Path(path).resolve()
		if not self.path.exists():
			raise FileNotFoundError(f"Statistics database not found: {self.path}")

	def connect(self):
		uri = f"file:{self.path.as_posix()}?mode=ro"
		conn = sqlite3.connect(uri, uri=True, timeout=30)
		conn.row_factory = sqlite3.Row
		conn.execute("PRAGMA query_only = ON")
		conn.execute("PRAGMA busy_timeout = 10000")
		return conn

	@staticmethod
	def table_exists(conn: sqlite3.Connection, name: str) -> bool:
		return conn.execute(
			"SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (name,),
		).fetchone() is not None

	def metadata(self) -> dict:
		with closing(self.connect()) as conn:
			has_guilds = self.table_exists(conn, "guilds")
			has_members = self.table_exists(conn, "members")
			guild_join = "LEFT JOIN guilds g ON g.guild_id = x.guild_id" if has_guilds else ""
			guild_name = "COALESCE(g.name, 'Server ' || x.guild_id)" if has_guilds else "'Server ' || x.guild_id"
			guilds = [dict(row) for row in conn.execute(
				f"""
				SELECT x.guild_id, {guild_name} AS name, x.messages
				FROM (
					SELECT guild_id, COUNT(*) AS messages
					FROM messages GROUP BY guild_id
				) x
				{guild_join}
				ORDER BY x.messages DESC
				"""
			)]
			channels = [dict(row) for row in conn.execute(
				"""
				SELECT c.channel_id, c.guild_id, c.parent_channel_id, c.name, c.kind,
					p.name AS parent_name
				FROM channels c
				LEFT JOIN channels p ON p.channel_id = c.parent_channel_id
				ORDER BY c.guild_id, c.kind, c.name COLLATE NOCASE
				"""
			)]
			if has_members:
				members = [dict(row) for row in conn.execute(
					"""
					SELECT guild_id, user_id, username, display_name, avatar_url
					FROM members
					ORDER BY display_name COLLATE NOCASE
					LIMIT 10000
					"""
				)]
			else:
				members = []
			return {"guilds": guilds, "channels": channels, "members": members}

	@staticmethod
	def filters(
		params: dict[str, list[str]],
		*,
		daily: bool = False,
	) -> tuple[str, list[int]]:
		guild_id = integer(params.get("guild_id", [None])[0])
		if guild_id is None:
			raise ValueError("guild_id is required")
		where = ["m.guild_id = ?"]
		values = [guild_id]
		start_column = "m.day >= date(?, 'unixepoch')" if daily else "m.created_at >= ?"
		end_column = "m.day < date(?, 'unixepoch')" if daily else "m.created_at < ?"
		for key, column in (
			("start", start_column),
			("end", end_column),
			("forum_id", "m.parent_channel_id = ?"),
			("member_id", "m.user_id = ?"),
		):
			value = integer(params.get(key, [None])[0])
			if value is not None:
				where.append(column)
				values.append(value)
		channel_id = integer(params.get("channel_id", [None])[0])
		if channel_id is not None:
			where.append("(m.channel_id = ? OR m.parent_channel_id = ?)")
			values.extend((channel_id, channel_id))
		return " AND ".join(where), values

	def report(self, params: dict[str, list[str]]) -> dict:
		raw_where, raw_values = self.filters(params)
		start = integer(params.get("start", [None])[0])
		end = integer(params.get("end", [None])[0])
		if start is not None and end is not None and start >= end:
			raise ValueError("Start must be before end")
		now = int(datetime.now(timezone.utc).timestamp())
		span = (end or now) - (start or 0)
		if start is None or span > 730 * 86400:
			bucket = "%Y-%m"
			bucket_label = "month"
		elif span > 180 * 86400:
			bucket = "%Y-%W"
			bucket_label = "week"
		else:
			bucket = "%Y-%m-%d"
			bucket_label = "day"

		with closing(self.connect()) as conn:
			has_members = self.table_exists(conn, "members")
			has_daily = self.table_exists(conn, "daily_message_stats") and conn.execute(
				"SELECT 1 FROM stats_metadata WHERE key = 'daily_stats_version' AND value = '1'"
			).fetchone() is not None
			where, values = self.filters(params, daily=has_daily)
			source = "daily_message_stats m" if has_daily else "messages m"
			total = "COALESCE(SUM(m.message_count), 0)" if has_daily else "COUNT(*)"
			first_message = (
				"CAST(strftime('%s', MIN(m.day)) AS INTEGER)"
				if has_daily else "MIN(m.created_at)"
			)
			last_message = (
				"CAST(strftime('%s', MAX(m.day)) AS INTEGER) + 86399"
				if has_daily else "MAX(m.created_at)"
			)
			active_day = "m.day" if has_daily else "date(m.created_at, 'unixepoch')"
			if has_daily:
				bucket_expression = (
					"substr(m.day, 1, 7)" if bucket == "%Y-%m"
					else "strftime('%Y-%W', m.day)" if bucket == "%Y-%W"
					else "m.day"
				)
			else:
				bucket_expression = f"strftime('{bucket}', m.created_at, 'unixepoch')"
			member_join = (
				"LEFT JOIN members u ON u.guild_id = m.guild_id AND u.user_id = m.user_id"
				if has_members else ""
			)
			member_name = "COALESCE(u.display_name, u.username, 'User ' || m.user_id)" if has_members else "'User ' || m.user_id"
			member_avatar = "MAX(u.avatar_url)" if has_members else "NULL"

			summary = dict(conn.execute(
				f"""
				SELECT {total} AS messages,
					COUNT(DISTINCT m.user_id) AS contributors,
					COUNT(DISTINCT CASE
						WHEN summary_channel.kind = 'thread'
						THEN summary_channel.parent_channel_id
						ELSE m.channel_id
					END) AS locations,
					{first_message} AS first_message_at,
					{last_message} AS last_message_at,
					COUNT(DISTINCT {active_day}) AS active_days
				FROM {source}
				LEFT JOIN channels summary_channel
					ON summary_channel.channel_id = m.channel_id
				WHERE {where}
				""",
				values,
			).fetchone())

			timeline = [dict(row) for row in conn.execute(
				f"""
				SELECT {bucket_expression} AS bucket,
					{total} AS messages,
					COUNT(DISTINCT m.user_id) AS contributors
				FROM {source} WHERE {where}
				GROUP BY bucket ORDER BY bucket
				""",
				values,
			)]

			top_members = [dict(row) for row in conn.execute(
				f"""
				SELECT m.user_id, {member_name} AS name, {member_avatar} AS avatar_url,
					{total} AS messages, COUNT(DISTINCT CASE
						WHEN member_channel.kind = 'thread'
						THEN member_channel.parent_channel_id
						ELSE m.channel_id
					END) AS locations
				FROM {source} {member_join}
				LEFT JOIN channels member_channel
					ON member_channel.channel_id = m.channel_id
				WHERE {where}
				GROUP BY m.user_id
				ORDER BY messages DESC, m.user_id
				LIMIT 25
				""",
				values,
			)]

			top_locations = [dict(row) for row in conn.execute(
				f"""
				SELECT display.channel_id, display.name, display.kind,
					display.parent_channel_id, display_parent.name AS parent_name,
					{total} AS messages,
					COUNT(DISTINCT m.user_id) AS contributors
				FROM {source}
				JOIN channels actual ON actual.channel_id = m.channel_id
				JOIN channels display ON display.channel_id = CASE
					WHEN actual.kind = 'thread' THEN actual.parent_channel_id
					ELSE actual.channel_id
				END
				LEFT JOIN channels display_parent
					ON display_parent.channel_id = display.parent_channel_id
				WHERE {where}
				GROUP BY display.channel_id
				ORDER BY messages DESC, display.channel_id
				LIMIT 25
				""",
				values,
			)]

			forum_where = where + " AND p.kind = 'forum'"
			top_forums = [dict(row) for row in conn.execute(
				f"""
				SELECT m.parent_channel_id AS forum_id,
					COALESCE(p.name, 'Forum ' || m.parent_channel_id) AS name,
					{total} AS messages, COUNT(DISTINCT m.channel_id) AS threads,
					COUNT(DISTINCT m.user_id) AS contributors
				FROM {source}
				LEFT JOIN channels p ON p.channel_id = m.parent_channel_id
				WHERE {forum_where}
				GROUP BY m.parent_channel_id
				ORDER BY messages DESC, m.parent_channel_id
				LIMIT 25
				""",
				values,
			)]

			forum_threads = [dict(row) for row in conn.execute(
				f"""
				SELECT m.channel_id, c.name, c.kind, c.parent_channel_id,
					p.name AS parent_name, {total} AS messages,
					COUNT(DISTINCT m.user_id) AS contributors
				FROM {source}
				JOIN channels c ON c.channel_id = m.channel_id
				LEFT JOIN channels p ON p.channel_id = m.parent_channel_id
				WHERE {forum_where}
				GROUP BY m.channel_id
				ORDER BY messages DESC, m.channel_id
				LIMIT 25
				""",
				values,
			)]

			heatmap = [dict(row) for row in conn.execute(
				f"""
				SELECT CAST(strftime('%w', m.created_at, 'unixepoch') AS INTEGER) AS weekday,
					CAST(strftime('%H', m.created_at, 'unixepoch') AS INTEGER) AS hour,
					COUNT(*) AS messages
				FROM messages m WHERE {raw_where}
				GROUP BY weekday, hour
				""",
				raw_values,
			)]

			peak = max(timeline, key=lambda item: item["messages"], default=None)
			summary["average_per_active_day"] = round(
				summary["messages"] / summary["active_days"]
			) if summary["active_days"] else 0
			summary["peak_bucket"] = peak
			return {
				"summary": summary,
				"timeline": timeline,
				"bucket": bucket_label,
				"top_members": top_members,
				"top_locations": top_locations,
				"top_forums": top_forums,
				"forum_threads": forum_threads,
				"heatmap": heatmap,
				"generated_at": now,
				"accelerated": has_daily,
			}


class ResponseCache:
	def __init__(self, ttl: int = 15):
		self.ttl = ttl
		self.values: dict[str, tuple[float, dict]] = {}

	def get(self, key: str) -> dict | None:
		item = self.values.get(key)
		if item and time.monotonic() - item[0] < self.ttl:
			return item[1]
		return None

	def put(self, key: str, value: dict):
		self.values[key] = (time.monotonic(), value)
		if len(self.values) > 100:
			oldest = min(self.values, key=lambda entry: self.values[entry][0])
			del self.values[oldest]


class DashboardHandler(BaseHTTPRequestHandler):
	protocol_version = "HTTP/1.1"
	repository: StatsRepository
	access_token: str | None
	cache = ResponseCache()

	def log_message(self, fmt: str, *args):
		print(f"{self.log_date_time_string()} {self.address_string()} {fmt % args}")

	def authenticated(self) -> bool:
		if not self.access_token:
			return True
		supplied = self.headers.get("X-Stats-Token", "")
		return hmac.compare_digest(supplied, self.access_token)

	def json_response(self, payload: dict, status: int = HTTPStatus.OK):
		body = json.dumps(json_safe_ids(payload), separators=(",", ":")).encode("utf-8")
		self.send_response(status)
		self.send_header("Content-Type", "application/json; charset=utf-8")
		self.send_header("Content-Length", str(len(body)))
		self.send_header("Cache-Control", "no-store")
		self.send_header("X-Content-Type-Options", "nosniff")
		self.send_header("Referrer-Policy", "no-referrer")
		self.end_headers()
		self.wfile.write(body)

	def serve_static(self, request_path: str):
		relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
		path = (STATIC_ROOT / relative).resolve()
		if not path.is_relative_to(STATIC_ROOT) or not path.is_file():
			self.send_error(HTTPStatus.NOT_FOUND)
			return
		body = path.read_bytes()
		content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
		self.send_response(HTTPStatus.OK)
		self.send_header("Content-Type", content_type)
		self.send_header("Content-Length", str(len(body)))
		self.send_header("Cache-Control", "no-cache")
		self.send_header("X-Content-Type-Options", "nosniff")
		self.send_header("X-Frame-Options", "DENY")
		self.send_header("Referrer-Policy", "no-referrer")
		self.send_header(
			"Content-Security-Policy",
			"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
			"img-src 'self' https: data:; connect-src 'self'; frame-ancestors 'none'",
		)
		self.end_headers()
		self.wfile.write(body)

	def do_GET(self):
		parsed = urlparse(self.path)
		if parsed.path.startswith("/api/"):
			if not self.authenticated():
				self.json_response({"error": "Access token required"}, HTTPStatus.UNAUTHORIZED)
				return
			try:
				if parsed.path == "/api/meta":
					cache_key = "meta"
					payload = self.cache.get(cache_key)
					if payload is None:
						payload = self.repository.metadata()
						self.cache.put(cache_key, payload)
					self.json_response(payload)
					return
				if parsed.path == "/api/report":
					params = parse_qs(parsed.query)
					cache_key = hashlib.sha256(parsed.query.encode()).hexdigest()
					payload = self.cache.get(cache_key)
					if payload is None:
						payload = self.repository.report(params)
						self.cache.put(cache_key, payload)
					self.json_response(payload)
					return
				self.json_response({"error": "Unknown endpoint"}, HTTPStatus.NOT_FOUND)
			except ValueError as exc:
				self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
			except sqlite3.Error as exc:
				print(f"Database error: {exc}")
				self.json_response({"error": "The statistics database is busy or unavailable."}, HTTPStatus.SERVICE_UNAVAILABLE)
			return
		self.serve_static(parsed.path)


def run():
	database = os.getenv("MESSAGE_STATS_DATABASE", str(DEFAULT_DATABASE))
	host = os.getenv("MESSAGE_STATS_VIEWER_HOST", "127.0.0.1")
	port = int(os.getenv("MESSAGE_STATS_VIEWER_PORT", "8765"))
	token = os.getenv("MESSAGE_STATS_VIEWER_TOKEN")
	if host not in {"127.0.0.1", "localhost", "::1"} and not token:
		raise RuntimeError(
			"Set MESSAGE_STATS_VIEWER_TOKEN before binding the dashboard beyond localhost."
		)
	DashboardHandler.repository = StatsRepository(database)
	DashboardHandler.access_token = token
	server = ThreadingHTTPServer((host, port), DashboardHandler)
	print(f"Message Stats dashboard: http://{host}:{port}")
	print(f"Database: {Path(database).resolve()}")
	try:
		server.serve_forever()
	except KeyboardInterrupt:
		print("Stopping dashboard")
	finally:
		server.server_close()


if __name__ == "__main__":
	run()
