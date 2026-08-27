import discord

from core.database import db


def is_admin(member: discord.Member) -> bool:
	return member.guild_permissions.administrator


def can_manage_bot(member: discord.Member) -> bool:
	if is_admin(member):
		return True
	manager_roles = db.get_manager_roles(member.guild.id)
	return any(role.id in manager_roles for role in member.roles)


def can_moderate_bot(member: discord.Member, required_level: int = 1) -> bool:
	if can_manage_bot(member):
		return True
	moderator_roles = db.get_moderator_roles(member.guild.id)
	return any(
		moderator_roles.get(role.id, required_level + 1) <= required_level
		for role in member.roles
	)


async def require_admin(interaction: discord.Interaction) -> bool:
	if interaction.guild and isinstance(interaction.user, discord.Member) and is_admin(interaction.user):
		return True
	await interaction.response.send_message(
		"Only server administrators can use this command.",
		ephemeral=True,
	)
	return False


async def require_manager(interaction: discord.Interaction) -> bool:
	if (
		interaction.guild
		and isinstance(interaction.user, discord.Member)
		and can_manage_bot(interaction.user)
	):
		return True
	await interaction.response.send_message(
		"You need Administrator or an approved manager role to change this bot's setup.",
		ephemeral=True,
	)
	return False


async def require_moderator(
	interaction: discord.Interaction,
	required_level: int = 1,
) -> bool:
	if (
		interaction.guild
		and isinstance(interaction.user, discord.Member)
		and can_moderate_bot(interaction.user, required_level)
	):
		return True
	await interaction.response.send_message(
		"You need Administrator, an approved manager role, or a moderator role "
		f"with permission level {required_level} or a lower-numbered level to use "
		"this command.",
		ephemeral=True,
	)
	return False
