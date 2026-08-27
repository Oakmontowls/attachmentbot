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
		db.add_temporary_role(member.guild.id, member.id, role.id, 0, reason)
		return

	expires_at = int(time.time()) + duration_seconds
	await member.add_roles(role, reason=reason)
	db.add_temporary_role(member.guild.id, member.id, role.id, expires_at, reason)


async def remove_expired_temporary_roles(bot):
	now = int(time.time())
	removed = 0
	for row in db.get_due_temporary_roles(now):
		guild = bot.get_guild(row["guild_id"])
		if not guild:
			continue

		role = guild.get_role(row["role_id"])
		member = guild.get_member(row["user_id"])
		if not member:
			try:
				member = await guild.fetch_member(row["user_id"])
			except discord.NotFound:
				member = None
			except discord.DiscordException as exc:
				log.info(
					f"[TEMP ROLE MEMBER ERROR] guild={row['guild_id']} "
					f"user={row['user_id']} error={exc}"
				)
				continue

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
		removed += 1
	return removed


async def verify_temporary_roles(bot):
	removed = await remove_expired_temporary_roles(bot)
	stale = 0
	for row in db.get_temporary_roles():
		guild = bot.get_guild(row["guild_id"])
		if not guild:
			continue
		role = guild.get_role(row["role_id"])
		member = guild.get_member(row["user_id"])
		if not member:
			try:
				member = await guild.fetch_member(row["user_id"])
			except discord.NotFound:
				member = None
			except discord.DiscordException as exc:
				log.info(
					f"[TEMP ROLE VERIFY MEMBER ERROR] guild={row['guild_id']} "
					f"user={row['user_id']} error={exc}"
				)
				continue
		if not role or not member or role not in member.roles:
			db.remove_temporary_role(row["guild_id"], row["user_id"], row["role_id"])
			stale += 1

	untracked = 0
	tracked = {
		(row["guild_id"], row["user_id"], row["role_id"])
		for row in db.get_temporary_roles()
	}
	for guild in bot.guilds:
		if not guild.chunked:
			try:
				await guild.chunk(cache=True)
			except discord.DiscordException as exc:
				log.info(f"[TEMP ROLE VERIFY CHUNK ERROR] guild={guild.id} error={exc}")
		settings = db.get_shared_settings(guild.id)
		role_id = settings["role_id"] if settings else None
		role = guild.get_role(role_id) if role_id else None
		if not role:
			continue
		for member in role.members:
			if (guild.id, member.id, role.id) not in tracked:
				untracked += 1

	log.info(
		f"[TEMP ROLE VERIFY] expired_removed={removed} stale_records={stale} "
		f"untracked_role_holders={untracked}"
	)


async def start_temporary_role_worker(bot, interval_seconds: int = 30):
	log.info("[TEMP ROLE] Worker started")
	await bot.wait_until_ready()
	await verify_temporary_roles(bot)

	while True:
		await asyncio.sleep(interval_seconds)
		await remove_expired_temporary_roles(bot)
