import discord
from utils.ocr import run_ocr
from utils.hashing import phash_bytes
from utils.keywords import score
import aiohttp
import asyncio
from core.logger import log
from core.database import db, FEATURE_OCR_DELETE_MESSAGE, FEATURE_OCR_GIVE_ROLE


async def download_image(url):
	async with aiohttp.ClientSession() as session:
		async with session.get(url) as resp:
			if resp.status == 200:
				return await resp.read()
	return None


async def process_message_pipeline(bot, message: discord.Message):

	config = db.get_guild_settings(message.guild.id)
	if not config or not config["enabled"]:
		return

	log_channel = bot.get_channel(config["log_channel_id"]) if config["log_channel_id"] else None
	
	role = message.guild.get_role(config["role_id"]) if config["role_id"] else None

	threshold = config.get("detection_threshold", 10)
	keywords = config.get("keywords", {})
	if not keywords:
		log.info(f"[SKIP] guild={message.guild.id} has no keywords configured")
		return

	all_hits = {}
	matched = False

	loop = asyncio.get_running_loop()

	image_attachments = [
		att for att in message.attachments
		if att.content_type and att.content_type.startswith("image/")
	]

	for idx, att in enumerate(image_attachments):

		image_bytes = await download_image(att.url)
		if not image_bytes:
			continue

		ph = phash_bytes(image_bytes)

		text = await loop.run_in_executor(
			None,
			run_ocr,
			image_bytes
		)

		score_value, hits = score(text, keywords)

		all_hits.update(hits)

		log.info(f"[IMG {idx}] score={score_value} hash={ph}")

		if score_value >= threshold:
			matched = True
			break

	if not matched:
		return

	embed = discord.Embed(
		title="Suspicious Attachment Detected",
		color=discord.Color.red(),
		description=(
			f"User: {message.author.mention}\n"
			f"Channel: {message.channel.mention}\n"
			f"Message ID: {message.id}"
		)
	)

	if all_hits:
		embed.add_field(
			name="Matched Keywords",
			value=", ".join(
				f"{k} ({v})"
				for k, v in sorted(all_hits.items(), key=lambda x: x[1], reverse=True)
			),
			inline=False
		)
	else:
		embed.add_field(
			name="Matched Keywords",
			value="None",
			inline=False
		)

	# ---------------- ATTACHMENTS ----------------
	if image_attachments:
		# Show first image as embed preview
		embed.set_image(url=image_attachments[0].url)

		# List all attachment URLs
		attachment_text = "\n".join(
			f"{i+1}. {att.url}"
			for i, att in enumerate(image_attachments)
		)

		embed.add_field(
			name="Attachments",
			value=attachment_text[:1024],
			inline=False
		)
	if db.is_feature_enabled(message.guild.id, FEATURE_OCR_GIVE_ROLE):
		if role:
			try:
				await message.author.add_roles(role, reason="OCR detection")
			except discord.DiscordException as exc:
				log.info(f"[ROLE ERROR] message={message.id} error={exc}")
		else:
			log.info("[ROLE SKIP] No role set")

	if db.is_feature_enabled(message.guild.id, FEATURE_OCR_DELETE_MESSAGE):
		try:
			await message.delete()
		except discord.DiscordException as exc:
			log.info(f"[DELETE ERROR] message={message.id} error={exc}")

	# ---------------- SEND ----------------
	if log_channel:
		await log_channel.send(embed=embed)
