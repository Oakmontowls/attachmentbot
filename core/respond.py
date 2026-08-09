import random
import re
import time

import discord

from core.database import db
from core.logger import log


NO_MENTIONS = discord.AllowedMentions.none()


class RespondService:
	def __init__(self):
		self.bot = None
		self._regex_cache = {}

	def start(self, bot):
		self.bot = bot

	def stop(self):
		self.bot = None
		self._regex_cache.clear()

	def _matches(self, trigger: dict, content: str) -> bool:
		if not trigger["is_regex"]:
			return trigger["trigger_text"].casefold() in content.casefold()

		cache_key = (trigger["trigger_id"], trigger["trigger_text"])
		pattern = self._regex_cache.get(cache_key)
		if pattern is None:
			try:
				pattern = re.compile(trigger["trigger_text"], re.IGNORECASE)
			except re.error as exc:
				log.info(
					f"[RESPOND REGEX ERROR] trigger={trigger['trigger_id']} error={exc}"
				)
				return False
			self._regex_cache[cache_key] = pattern
		return pattern.search(content) is not None

	async def _send_response(
		self,
		message: discord.Message,
		trigger: dict,
		trigger_kind: str,
		responded: set[int],
	):
		response_id = trigger["response_id"]
		if response_id in responded or random.random() >= trigger["response_chance"]:
			return
		responded_at = time.time()
		if trigger_kind == "sticker":
			trigger_id = trigger["sticker_trigger_id"]
			claimed = db.claim_auto_response_sticker_trigger(trigger_id, responded_at)
		else:
			trigger_id = trigger["trigger_id"]
			claimed = db.claim_auto_response_trigger(trigger_id, responded_at)
		if not claimed:
			return

		try:
			await message.channel.send(
				trigger["response_text"],
				allowed_mentions=NO_MENTIONS,
			)
		except discord.DiscordException as exc:
			log.info(
				f"[RESPOND ERROR] guild={message.guild.id} kind={trigger_kind} "
				f"trigger={trigger_id} message={message.id} error={exc}"
			)
			return

		responded.add(response_id)
		log.info(
			f"[RESPOND SENT] guild={message.guild.id} kind={trigger_kind} "
			f"trigger={trigger_id} message={message.id}"
		)

	async def handle_message(self, message: discord.Message):
		if not self.bot or not message.guild or message.author.bot:
			return
		if not message.content and not message.stickers:
			return

		responded = set()
		if message.content:
			for trigger in db.get_active_response_triggers(message.guild.id):
				if self._matches(trigger, message.content):
					await self._send_response(message, trigger, "text", responded)

		sticker_ids = {sticker.id for sticker in message.stickers}
		if sticker_ids:
			for trigger in db.get_active_response_sticker_triggers(message.guild.id):
				if trigger["sticker_id"] in sticker_ids:
					await self._send_response(message, trigger, "sticker", responded)


respond_service = RespondService()
