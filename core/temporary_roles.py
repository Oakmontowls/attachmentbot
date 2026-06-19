import asyncio
import time

import discord

from core.database import db
from core.logger import log


async def assign_temporary_role(
	member: discord.Member,
	role: discord.Role,
	duration_seconds: int,
	reason: str,
):
	if duration_seconds <= 0:
		await member.add_roles(role, reason=reason)
		return

	expires_at = int(time.time()) + duration_seconds
	await member.add_roles(role, reason=reason)
	db.add_temporary_role(member.guild.id, member.id, role.id, expires_at, reason)


async def remove_expired_temporary_roles(bot):
	now = int(time.time())
	for row in db.get_due_temporary_roles(now):
		guild = bot.get_guild(row["guild_id"])
		if not guild:
			continue

		role = guild.get_role(row["role_id"])
		member = guild.get_member(row["user_id"])
		if not member:
			try:
				member = await guild.fetch_member(row["user_id"])
			except discord.DiscordException:
				member = None

		try:
			if member and role and role in member.roles:
				await member.remove_roles(role, reason=f"Temporary role expired: {row['reason']}")
		except discord.DiscordException as exc:
			log.info(
				f"[TEMP ROLE ERROR] guild={row['guild_id']} user={row['user_id']} "
				f"role={row['role_id']} error={exc}"
			)
			continue

		db.remove_temporary_role(row["guild_id"], row["user_id"], row["role_id"])


async def start_temporary_role_worker(bot, interval_seconds: int = 30):
	log.info("[TEMP ROLE] Worker started")
	await bot.wait_until_ready()

	while True:
		await remove_expired_temporary_roles(bot)
		await asyncio.sleep(interval_seconds)
