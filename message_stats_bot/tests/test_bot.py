import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

from message_stats_bot.bot import (
	channel_created_at,
	configure_event_loop,
	migrated_embed_author,
	migrated_embed_records,
	migrated_message_id,
)


class ChannelMetadataTests(unittest.TestCase):
	def test_event_loop_configuration_is_safe_on_current_platform(self):
		configure_event_loop()

	def test_migrated_embed_extracts_avatar_user_and_local_timestamp(self):
		embed = discord.Embed(
			description="`6/5/2025` `3:45 PM`\nAn old forum post",
		)
		embed.set_author(
			name="Cephalon_Sark",
			icon_url=(
				"https://images-ext-1.discordapp.net/external/hash/https/"
				"cdn.discordapp.com/avatars/195564283084406784/avatar.png"
			),
		)
		parsed = migrated_embed_author(embed, ZoneInfo("America/Los_Angeles"))
		self.assertIsNotNone(parsed)
		user_id, name, avatar_url, created_at = parsed
		self.assertEqual(user_id, 195564283084406784)
		self.assertEqual(name, "Cephalon_Sark")
		self.assertIn("/avatars/195564283084406784/", avatar_url)
		self.assertEqual(
			created_at,
			int(datetime(2025, 6, 5, 22, 45, tzinfo=timezone.utc).timestamp()),
		)

	def test_each_migrated_embed_gets_a_stable_negative_id(self):
		self.assertLess(migrated_message_id(123, 0), 0)
		self.assertEqual(migrated_message_id(123, 0), migrated_message_id(123, 0))
		self.assertNotEqual(migrated_message_id(123, 0), migrated_message_id(123, 1))

	def test_same_author_continuation_inherits_previous_timestamp(self):
		zone = ZoneInfo("America/Los_Angeles")
		first = discord.Embed(description="`6/5/2025` `3:45 PM`\nFirst")
		first.set_author(
			name="Cephalon_Sark",
			icon_url="https://cdn.discordapp.com/avatars/195564283084406784/a.png",
		)
		continuation = discord.Embed(description="Second message without a timestamp")
		continuation.set_author(
			name="Cephalon_Sark",
			icon_url="https://cdn.discordapp.com/avatars/195564283084406784/a.png",
		)

		first_records, state = migrated_embed_records(100, [first], zone)
		continued_records, continued_state = migrated_embed_records(
			101, [continuation], zone, state
		)
		self.assertEqual(len(first_records), 1)
		self.assertEqual(len(continued_records), 1)
		self.assertEqual(continued_records[0][4], first_records[0][4])
		self.assertEqual(continued_state, state)

	def test_missing_timestamp_does_not_cross_authors(self):
		embed = discord.Embed(description="No timestamp")
		embed.set_author(
			name="SomeoneElse",
			icon_url="https://cdn.discordapp.com/avatars/222222222222222222/a.png",
		)
		records, state = migrated_embed_records(
			102,
			[embed],
			ZoneInfo("UTC"),
			(195564283084406784, 12345),
		)
		self.assertEqual(records, [])
		self.assertEqual(state, (195564283084406784, 12345))

	def test_missing_created_at_is_derived_from_snowflake(self):
		expected = datetime(2024, 5, 20, 12, 30, tzinfo=timezone.utc)
		channel = SimpleNamespace(
			id=discord.utils.time_snowflake(expected),
			created_at=None,
		)
		actual = channel_created_at(channel)
		self.assertEqual(int(actual.timestamp()), int(expected.timestamp()))

	def test_existing_created_at_is_preserved(self):
		expected = datetime(2020, 1, 2, tzinfo=timezone.utc)
		channel = SimpleNamespace(id=1, created_at=expected)
		self.assertIs(channel_created_at(channel), expected)


if __name__ == "__main__":
	unittest.main()
