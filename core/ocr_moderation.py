import asyncio
from collections import defaultdict

import discord

from core.database import db, MODERATION_PERMISSION_OCR_REVIEW
from core.logger import log
from core.permissions import can_moderate_bot


AUDIT_REASON = "Compromised or hacked account"
NO_MENTIONS = discord.AllowedMentions.none()
ACTION_LABELS = {
	"ban": "Ban",
	"kick_with_message": "Kick with message",
	"ignore": "Ignore",
}


class OcrModerationService:
	def __init__(self):
		self.bot = None
		self._review_locks = defaultdict(asyncio.Lock)
		self._review_creation_locks = defaultdict(asyncio.Lock)

	def start(self, bot):
		self.bot = bot

	def stop(self):
		self.bot = None
		self._review_locks.clear()
		self._review_creation_locks.clear()

	async def _resolve_channel(self, channel_id: int | None):
		if not self.bot or not channel_id:
			return None
		channel = self.bot.get_channel(channel_id)
		if channel:
			return channel
		try:
			return await self.bot.fetch_channel(channel_id)
		except discord.DiscordException:
			return None

	async def _resolve_member(self, guild: discord.Guild, user_id: int):
		member = guild.get_member(user_id)
		if member:
			return member
		try:
			return await guild.fetch_member(user_id)
		except discord.DiscordException:
			return None

	async def _resolve_user(self, user_id: int):
		if not self.bot:
			return None
		user = self.bot.get_user(user_id)
		if user:
			return user
		try:
			return await self.bot.fetch_user(user_id)
		except discord.DiscordException:
			return None

	async def _remove_moderation_role(self, guild: discord.Guild, user_id: int) -> bool:
		shared = db.get_shared_settings(guild.id)
		role_id = shared["role_id"] if shared else None
		if not role_id:
			return False
		role = guild.get_role(role_id)
		member = await self._resolve_member(guild, user_id)
		if member and role and role in member.roles:
			await member.remove_roles(role, reason=AUDIT_REASON)
			db.remove_temporary_role(guild.id, user_id, role_id)
			return True
		db.remove_temporary_role(guild.id, user_id, role_id)
		return False

	async def _validate_kick_with_message_permissions(
		self,
		guild: discord.Guild,
		target_user_id: int,
	):
		bot_member = guild.me
		if not bot_member and self.bot and self.bot.user:
			bot_member = guild.get_member(self.bot.user.id)
		if not bot_member:
			raise RuntimeError("The bot could not resolve its server member permissions.")
		if not bot_member.guild_permissions.ban_members:
			raise RuntimeError("The bot does not have the Ban Members permission.")

		target_member = await self._resolve_member(guild, target_user_id)
		if target_member:
			if target_member.id == guild.owner_id:
				raise RuntimeError("The server owner cannot be moderated.")
			if target_member.top_role >= bot_member.top_role:
				raise RuntimeError(
					"The bot's highest role must be above the detected member's highest role."
				)

		shared = db.get_shared_settings(guild.id)
		role_id = shared["role_id"] if shared else None
		role = guild.get_role(role_id) if role_id else None
		if target_member and role and role in target_member.roles:
			if not bot_member.guild_permissions.manage_roles:
				raise RuntimeError("The bot does not have the Manage Roles permission.")
			if role >= bot_member.top_role:
				raise RuntimeError(
					"The bot's highest role must be above the configured timeout role."
				)

	async def perform_action(
		self,
		guild: discord.Guild,
		target_user_id: int,
		action: str,
		direct_message: str | None,
	) -> dict:
		details = {"dm_sent": False, "role_removed": False}
		if action == "ignore":
			details["role_removed"] = await self._remove_moderation_role(
				guild, target_user_id,
			)
			return details

		target = discord.Object(id=target_user_id)
		if action == "ban":
			await guild.ban(target, reason=AUDIT_REASON, delete_message_seconds=0)
			return details

		if action != "kick_with_message":
			raise ValueError("Unknown OCR moderation action.")
		if not direct_message:
			raise RuntimeError("No OCR moderation direct message is configured.")
		await self._validate_kick_with_message_permissions(guild, target_user_id)

		user = await self._resolve_user(target_user_id)
		if user:
			try:
				await user.send(direct_message)
				details["dm_sent"] = True
			except discord.DiscordException as exc:
				log.info(
					f"[OCR MODERATION DM ERROR] guild={guild.id} "
					f"user={target_user_id} error={exc}"
				)

		try:
			details["role_removed"] = await self._remove_moderation_role(
				guild, target_user_id,
			)
		except discord.DiscordException as exc:
			log.info(
				f"[OCR MODERATION ROLE ERROR] guild={guild.id} "
				f"user={target_user_id} error={exc}"
			)

		await guild.ban(target, reason=AUDIT_REASON, delete_message_seconds=0)
		await guild.unban(target, reason=AUDIT_REASON)
		return details

	async def log_action(
		self,
		guild: discord.Guild,
		target_user_id: int,
		action: str,
		actor_id: int,
		details: dict,
		source_message_id: int,
	):
		settings = db.get_ocr_settings(guild.id)
		log_channel = await self._resolve_channel(
			settings["log_channel_id"] if settings else None
		)
		action_label = ACTION_LABELS[action]
		embed = discord.Embed(title="OCR Moderation Action", color=discord.Color.orange())
		embed.add_field(name="Action", value=action_label, inline=True)
		embed.add_field(
			name="Target",
			value=f"<@{target_user_id}> (`{target_user_id}`)",
			inline=False,
		)
		embed.add_field(
			name="Actioned By",
			value=f"<@{actor_id}> (`{actor_id}`)",
			inline=False,
		)
		embed.add_field(name="Detected Message ID", value=str(source_message_id), inline=False)
		if action == "kick_with_message":
			embed.add_field(
				name="Direct Message",
				value="Sent" if details["dm_sent"] else "Could not be delivered",
				inline=True,
			)

		if log_channel:
			try:
				await log_channel.send(embed=embed, allowed_mentions=NO_MENTIONS)
				return
			except discord.DiscordException as exc:
				log.info(
					f"[OCR MODERATION LOG ERROR] guild={guild.id} "
					f"user={target_user_id} action={action} error={exc}"
				)
		log.info(
			f"[OCR MODERATION ACTION] guild={guild.id} user={target_user_id} "
			f"action={action} actor={actor_id} source_message={source_message_id}"
		)

	async def _send_review(
		self,
		message: discord.Message,
		config: dict,
		detection_log_message: discord.Message | None,
	):
		key = (message.guild.id, message.author.id)
		async with self._review_creation_locks[key]:
			existing = db.get_pending_ocr_moderation_review(*key)
			if existing:
				log.info(
					f"[OCR REVIEW DEDUPED] guild={message.guild.id} "
					f"user={message.author.id} message={message.id} "
					f"pending_review={existing['review_id']}"
				)
				return

			moderation = config["ocr_moderation"]
			channel_id = moderation["review_channel_id"] or config["log_channel_id"]
			channel = await self._resolve_channel(channel_id)
			if not channel:
				log.info(
					f"[OCR REVIEW SKIP] guild={message.guild.id} message={message.id} "
					"no review or OCR log channel is available"
				)
				return

			embed = discord.Embed(
				title="OCR Moderation Review",
				color=discord.Color.red(),
				description=(
					f"Detected user: {message.author.mention} (`{message.author.id}`)\n"
					f"Source channel: {message.channel.mention}\n"
					f"Detected message ID: `{message.id}`"
				),
			)
			if detection_log_message:
				embed.add_field(
					name="Detection Log",
					value=f"[Open OCR moderation log]({detection_log_message.jump_url})",
					inline=False,
				)
			review_message = await channel.send(
				embed=embed,
				view=OcrModerationReviewView(self),
				allowed_mentions=NO_MENTIONS,
			)
			db.create_ocr_moderation_review(
				message.guild.id,
				message.author.id,
				message.id,
				message.channel.id,
				review_message.id,
				channel.id,
			)

	async def handle_detection(
		self,
		message: discord.Message,
		config: dict,
		detection_log_message: discord.Message | None = None,
	):
		moderation = config["ocr_moderation"]
		automatic_action = moderation["automatic_action"]
		if automatic_action != "none":
			try:
				details = await self.perform_action(
					message.guild,
					message.author.id,
					automatic_action,
					moderation["direct_message"],
				)
				actor_id = self.bot.user.id if self.bot and self.bot.user else 0
				await self.log_action(
					message.guild,
					message.author.id,
					automatic_action,
					actor_id,
					details,
					message.id,
				)
			except (discord.DiscordException, RuntimeError, ValueError) as exc:
				log.info(
					f"[OCR AUTOMATIC MODERATION ERROR] guild={message.guild.id} "
					f"user={message.author.id} action={automatic_action} error={exc}"
				)
			return

		if moderation["review_enabled"]:
			try:
				await self._send_review(message, config, detection_log_message)
			except discord.DiscordException as exc:
				log.info(
					f"[OCR REVIEW SEND ERROR] guild={message.guild.id} "
					f"message={message.id} error={exc}"
				)

	async def handle_review_interaction(
		self,
		interaction: discord.Interaction,
		action: str,
	):
		if not interaction.guild or not interaction.message:
			await interaction.response.send_message(
				"This review action is only available in its server.", ephemeral=True,
			)
			return
		review = db.get_ocr_moderation_review(interaction.message.id)
		if not review or review["guild_id"] != interaction.guild.id:
			await interaction.response.send_message(
				"This OCR review is no longer available.", ephemeral=True,
			)
			return
		settings = db.get_ocr_moderation_settings(interaction.guild.id)
		required_level = db.get_moderation_permission_level(
			interaction.guild.id, MODERATION_PERMISSION_OCR_REVIEW,
		)
		is_moderator = (
			isinstance(interaction.user, discord.Member)
			and can_moderate_bot(interaction.user, required_level)
		)
		if not settings["review_enabled"] or not is_moderator:
			await interaction.response.send_message(
				"You need Administrator, an approved manager role, or a moderator role "
				f"with permission level {required_level} or a lower-numbered level to "
				"use OCR review actions.",
				ephemeral=True,
			)
			return

		async with self._review_locks[review["review_id"]]:
			review = db.get_ocr_moderation_review(interaction.message.id)
			if not review or review["status"] != "pending":
				await interaction.response.send_message(
					"This OCR review has already been handled.", ephemeral=True,
				)
				return
			await interaction.response.defer(ephemeral=True, thinking=True)
			try:
				details = await self.perform_action(
					interaction.guild,
					review["target_user_id"],
					action,
					settings["direct_message"],
				)
			except (discord.DiscordException, RuntimeError, ValueError) as exc:
				await interaction.followup.send(
					f"The moderation action failed: {exc}", ephemeral=True,
				)
				return

			db.complete_ocr_moderation_review(
				review["review_id"], action, interaction.user.id,
			)
			disabled_view = OcrModerationReviewView(self)
			for item in disabled_view.children:
				item.disabled = True
			try:
				await interaction.message.edit(view=disabled_view)
			except discord.DiscordException as exc:
				log.info(
					f"[OCR REVIEW EDIT ERROR] review={review['review_id']} error={exc}"
				)
			await self.log_action(
				interaction.guild,
				review["target_user_id"],
				action,
				interaction.user.id,
				details,
				review["source_message_id"],
			)
			await interaction.followup.send(
				f"{ACTION_LABELS[action]} completed for <@{review['target_user_id']}>.",
				ephemeral=True,
				allowed_mentions=NO_MENTIONS,
			)


class OcrModerationReviewView(discord.ui.View):
	def __init__(self, service: OcrModerationService):
		super().__init__(timeout=None)
		self.service = service

	@discord.ui.button(
		label="Ban",
		style=discord.ButtonStyle.danger,
		custom_id="attachmentbot:ocr_review:ban",
	)
	async def ban(self, interaction: discord.Interaction, button: discord.ui.Button):
		await self.service.handle_review_interaction(interaction, "ban")

	@discord.ui.button(
		label="Kick with message",
		style=discord.ButtonStyle.danger,
		custom_id="attachmentbot:ocr_review:kick_with_message",
	)
	async def kick_with_message(
		self,
		interaction: discord.Interaction,
		button: discord.ui.Button,
	):
		await self.service.handle_review_interaction(interaction, "kick_with_message")

	@discord.ui.button(
		label="Ignore",
		style=discord.ButtonStyle.secondary,
		custom_id="attachmentbot:ocr_review:ignore",
	)
	async def ignore(self, interaction: discord.Interaction, button: discord.ui.Button):
		await self.service.handle_review_interaction(interaction, "ignore")


ocr_moderation = OcrModerationService()
