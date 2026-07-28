import discord

from core.database import db


def is_admin(member: discord.Member) -> bool:
	return member.guild_permissions.administrator


def can_manage_bot(member: discord.Member) -> bool:
	if is_admin(member):
		return True
	manager_roles = db.get_manager_roles(member.guild.id)
	return any(role.id in manager_roles for role in member.roles)


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
