import tempfile
import unittest
from pathlib import Path

from message_stats_bot.database import StatsDatabase
from message_stats_bot.viewer import StatsRepository, json_safe_ids


class StatsRepositoryTests(unittest.TestCase):
	def test_text_channel_report_rolls_up_child_threads(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "stats.sqlite3"
			db = StatsDatabase(path)
			db.upsert_guild(1, "Test server")
			for channel_id, parent_id, name, kind in (
				(10, None, "general", "text"),
				(11, 10, "hidden child", "thread"),
			):
				db.upsert_channel(
					guild_id=1,
					channel_id=channel_id,
					parent_channel_id=parent_id,
					name=name,
					kind=kind,
					owner_id=None,
					created_at=1000,
					archived=False,
				)
			db.record_messages([
				(10, 1, 10, None, 7, 172800),
				(11, 1, 11, 10, 8, 172900),
			])
			db.close()

			report = StatsRepository(path).report({"guild_id": ["1"], "channel_id": ["10"]})
			self.assertEqual(report["summary"]["messages"], 2)
			self.assertEqual(report["summary"]["locations"], 1)
			self.assertEqual(len(report["top_locations"]), 1)
			self.assertEqual(report["top_locations"][0]["channel_id"], 10)
			self.assertEqual(report["top_locations"][0]["messages"], 2)
			self.assertEqual(report["top_forums"], [])

	def test_discord_ids_are_json_safe_strings(self):
		payload = json_safe_ids({
			"guild_id": 684601221998444590,
			"messages": 123,
			"rows": [{"channel_id": 1309394953415430154}],
		})
		self.assertEqual(payload["guild_id"], "684601221998444590")
		self.assertEqual(payload["rows"][0]["channel_id"], "1309394953415430154")
		self.assertEqual(payload["messages"], 123)

	def test_accelerated_report_supports_forum_rollup(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "stats.sqlite3"
			db = StatsDatabase(path)
			db.upsert_guild(1, "Test server")
			db.upsert_channel(
				guild_id=1,
				channel_id=20,
				parent_channel_id=None,
				name="A forum",
				kind="forum",
				owner_id=None,
				created_at=900,
				archived=False,
			)
			db.upsert_channel(
				guild_id=1,
				channel_id=100,
				parent_channel_id=20,
				name="A forum post",
				kind="forum_post",
				owner_id=7,
				created_at=1000,
				archived=False,
			)
			db.record_messages([
				(1, 1, 100, 20, 7, 172800),
				(2, 1, 100, 20, 8, 172900),
			])
			db.close()

			report = StatsRepository(path).report({"guild_id": ["1"], "forum_id": ["20"]})
			self.assertTrue(report["accelerated"])
			self.assertEqual(report["summary"]["messages"], 2)
			self.assertEqual(report["summary"]["contributors"], 2)
			self.assertEqual(report["forum_threads"][0]["channel_id"], 100)


if __name__ == "__main__":
	unittest.main()
