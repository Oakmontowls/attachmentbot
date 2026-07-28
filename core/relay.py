import asyncio
from collections import defaultdict

import discord

from core.database import db
from core.logger import log


MAX_FILES_PER_MESSAGE = 10
MAX_EMBEDS_PER_MESSAGE = 10
NO_MENTIONS = discord.AllowedMentions.none()


def copy_sendable_embed(embed: discord.Embed) -> discord.Embed:
	data = embed.to_dict()
	data.pop("provider", None)
	data.pop("video", None)
	data["type"] = "rich"
	return discord.Embed.from_dict(data)


class RelayService:
	def __init__(self):
		self.bot = None
		self._route_locks = defaultdict(asyncio.Lock)

	def start(self, bot):
		self.bot = bot

	def stop(self):
		self.bot = None
		self._route_locks.clear()

	async def resolve_channel(self, channel_id: int):
		if not self.bot:
			return None
		channel = self.bot.get_channel(channel_id)
		if channel:
			return channel
		try:
			return await self.bot.fetch_channel(channel_id)
		except discord.DiscordException:
			return None

	async def set_enabled(self, guild_id: int, enabled: bool):
		if enabled and not db.get_relay_routes(guild_id):
			raise ValueError("Add at least one relay route before enabling the module.")
		db.set_relay_enabled(guild_id, enabled)

	async def add_route(
		self,
		guild_id: int,
		source_channel: discord.TextChannel,
		target_thread: discord.Thread,
		created_by: int,
	) -> int:
		return db.add_relay_route(
			guild_id,
			source_channel.guild.id,
			source_channel.id,
			target_thread.id,
			created_by,
		)

	def remove_route(self, guild_id: int, route_id: int) -> bool:
		return db.remove_relay_route(guild_id, route_id)

	async def _download_files(self, message: discord.Message) -> list[discord.File]:
		files = []
		for attachment in message.attachments:
			try:
				files.append(await attachment.to_file(use_cached=True))
			except (discord.DiscordException, OSError) as exc:
				log.info(
					f"[RELAY ATTACHMENT ERROR] message={message.id} "
					f"filename={attachment.filename} error={exc}"
				)
		return files

	async def _prepare_thread(self, route: dict) -> discord.Thread:
		target = await self.resolve_channel(route["target_thread_id"])
		if not isinstance(target, discord.Thread):
			raise RuntimeError("Target forum thread is unavailable.")
		if target.guild.id != route["guild_id"]:
			raise RuntimeError("Target thread no longer belongs to the configured server.")
		if not isinstance(target.parent, discord.ForumChannel):
			raise RuntimeError("Target is no longer a forum post thread.")
		if target.locked:
			raise RuntimeError("Target forum thread is locked.")
		if target.archived:
			await target.edit(archived=False, reason="News relay delivery")
		return target

	async def _deliver(self, message: discord.Message, route: dict):
		route_id = route["route_id"]
		async with self._route_locks[route_id]:
			if db.relay_delivery_exists(route_id, message.id):
				return
			target = await self._prepare_thread(route)
			files = await self._download_files(message)
			embeds = [copy_sendable_embed(embed) for embed in message.embeds]
			content = message.content or None
			if not content and not files and not embeds:
				content = message.jump_url

			first_message = None
			try:
				file_offset = 0
				embed_offset = 0
				first_batch = True
				while first_batch or file_offset < len(files) or embed_offset < len(embeds):
					batch_files = files[file_offset:file_offset + MAX_FILES_PER_MESSAGE]
					batch_embeds = embeds[embed_offset:embed_offset + MAX_EMBEDS_PER_MESSAGE]
					sent = await target.send(
						content=content if first_batch else None,
						files=batch_files,
						embeds=batch_embeds,
						allowed_mentions=NO_MENTIONS,
					)
					if first_batch:
						first_message = sent
						db.mark_relay_delivery(route_id, message.id, sent.id)
					first_batch = False
					file_offset += len(batch_files)
					embed_offset += len(batch_embeds)
			finally:
				for file in files:
					file.close()

			if first_message:
				log.info(
					f"[RELAY DELIVERED] route={route_id} source={message.jump_url} "
					f"target={first_message.jump_url}"
				)

	async def handle_message(self, message: discord.Message):
		if not self.bot or not message.guild:
			return
		if self.bot.user and message.author.id == self.bot.user.id:
			return
		routes = db.get_active_relay_routes(message.channel.id)
		for route in routes:
			if route["source_guild_id"] != message.guild.id:
				continue
			try:
				await self._deliver(message, route)
			except (discord.DiscordException, RuntimeError, OSError) as exc:
				log.info(
					f"[RELAY ERROR] route={route['route_id']} message={message.id} error={exc}"
				)


relay_service = RelayService()
