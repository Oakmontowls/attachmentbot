import tempfile
import unittest
from pathlib import Path

from message_stats_bot.database import StatsDatabase


class StatsDatabaseTests(unittest.TestCase):
	def setUp(self):
		self.tempdir = tempfile.TemporaryDirectory()
		self.db = StatsDatabase(
			Path(self.tempdir.name) / "stats.sqlite3", migration_forum_id=20
		)
		for channel_id, parent_id, kind in (
			(10, None, "text"),
			(11, None, "voice_text"),
			(100, 20, "forum_post"),
			(101, 20, "forum_post"),
			(102, 30, "forum_post"),
		):
			self.db.upsert_channel(
				guild_id=1,
				channel_id=channel_id,
				parent_channel_id=parent_id,
				name=f"channel-{channel_id}",
				kind=kind,
				owner_id=None,
				created_at=1000,
				archived=False,
			)

	def tearDown(self):
		self.db.close()
		self.tempdir.cleanup()

	def record(self, message_id, channel_id, parent_id, user_id, created_at):
		return self.db.record_message(
			message_id=message_id,
			guild_id=1,
			channel_id=channel_id,
			parent_channel_id=parent_id,
			user_id=user_id,
			created_at=created_at,
		)

	def test_duplicate_messages_are_ignored(self):
		self.assertTrue(self.record(1, 10, None, 7, 2000))
		self.assertFalse(self.record(1, 10, None, 7, 2000))
		self.assertEqual(self.db.summary(1, None)["messages"], 1)
		self.assertEqual(self.db.latest_message_id(1, 10), 1)
		self.assertIsNone(self.db.latest_message_id(1, 999))

	def test_history_cursor_is_independent_of_live_messages(self):
		self.db.record_message(
			message_id=999,
			guild_id=1,
			channel_id=10,
			parent_channel_id=None,
			user_id=7,
			created_at=200,
		)
		self.assertIsNone(self.db.history_cursor(1, 10))

		self.db.update_history_cursor(1, 10, 123)
		self.assertEqual(self.db.history_cursor(1, 10), 123)

		self.db.update_history_cursor(
			1,
			10,
			456,
			complete=True,
			migration_user_id=99,
			migration_timestamp=1000,
		)
		self.assertEqual(self.db.history_cursor(1, 10), 456)
		self.assertEqual(self.db.history_state(1, 10), (456, 99, 1000))

		self.db.begin_history_scan(1, 10)
		row = self.db.conn.execute(
			"SELECT after_message_id, completed_at FROM history_imports WHERE channel_id = 10"
		).fetchone()
		self.assertEqual(row["after_message_id"], 456)
		self.assertIsNone(row["completed_at"])

		self.db.update_history_cursor(1, 10, 789, complete=True)
		row = self.db.conn.execute(
			"SELECT after_message_id, completed_at FROM history_imports WHERE channel_id = 10"
		).fetchone()
		self.assertEqual(row["after_message_id"], 789)
		self.assertIsNotNone(row["completed_at"])

	def test_forum_reattribution_rescan_runs_only_once(self):
		self.db.update_history_cursor(1, 100, 123, complete=True)
		self.db.update_history_cursor(1, 102, 789, complete=True)
		self.record(-1, 100, 20, 7, 2000)
		self.record(-2, 102, 30, 7, 2000)
		self.db.conn.execute(
			"DELETE FROM stats_metadata WHERE key = 'forum_reattribution_version'"
		)
		self.db.conn.commit()
		path = self.db.path
		self.db.close()

		self.db = StatsDatabase(path, migration_forum_id=20)
		self.assertIsNone(self.db.history_cursor(1, 100))
		self.assertEqual(self.db.history_cursor(1, 102), 789)
		self.assertEqual(
			self.db.conn.execute(
				"SELECT COUNT(*) FROM messages WHERE message_id < 0"
			).fetchone()[0],
			0,
		)
		self.db.update_history_cursor(1, 100, 456, complete=True)
		self.db.close()

		self.db = StatsDatabase(path, migration_forum_id=20)
		self.assertEqual(self.db.history_cursor(1, 100), 456)

	def test_forum_parent_rolls_up_post_threads(self):
		self.record(1, 100, 20, 7, 2000)
		self.record(2, 100, 20, 8, 3000)
		self.record(3, 101, 20, 7, 4000)
		self.record(4, 10, None, 7, 5000)

		forum = self.db.summary(1, None, parent_channel_id=20)
		self.assertEqual(forum["messages"], 3)
		self.assertEqual(forum["members"], 2)
		self.assertEqual(forum["channels"], 2)

	def test_voice_channel_text_messages_are_normal_message_rows(self):
		self.record(50, 11, None, 7, 6000)
		row = self.db.top_channels(1, None, channel_id=11)[0]
		self.assertEqual(row["kind"], "voice_text")
		self.assertEqual(row["messages"], 1)

	def test_batch_insert_is_atomic_and_deduplicated(self):
		rows = [
			(1, 1, 10, None, 7, 2000),
			(2, 1, 100, 20, 8, 3000),
			(2, 1, 100, 20, 8, 3000),
		]
		self.assertEqual(self.db.record_messages(rows), 2)
		self.assertEqual(self.db.record_messages(rows), 0)
		self.assertEqual(self.db.summary(1, None)["messages"], 2)
		daily = self.db.conn.execute(
			"SELECT SUM(message_count) FROM daily_message_stats WHERE guild_id = 1"
		).fetchone()[0]
		self.assertEqual(daily, 2)

	def test_scopes_cutoffs_ranks_and_leaderboards(self):
		self.record(1, 10, None, 8, 2000)
		self.record(2, 100, 20, 7, 3000)
		self.record(3, 101, 20, 7, 4000)

		self.assertEqual(self.db.summary(1, 2500)["messages"], 2)
		self.assertEqual(self.db.summary(1, None, user_id=7)["channels"], 2)
		self.assertEqual(self.db.member_rank(1, 7, None), 1)
		self.assertEqual(self.db.top_users(1, None)[0]["user_id"], 7)
		self.assertEqual(
			self.db.top_channels(1, None, parent_channel_id=20)[0]["channel_id"],
			100,
		)


if __name__ == "__main__":
	unittest.main()
