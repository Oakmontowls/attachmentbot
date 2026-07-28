from discord import app_commands


async def attach_feature_cog(bot, cog):
	await bot.add_cog(cog)
	group = cog.__cog_app_commands_group__
	bot.tree.remove_command(group.name)
	root = bot.tree.get_command("ab")
	if not isinstance(root, app_commands.Group):
		await bot.remove_cog(cog.qualified_name)
		raise RuntimeError("The AttachmentBot base cog must load before feature cogs.")
	group.parent = root
	root.add_command(group)
	cog._attachmentbot_root = root


def detach_feature_cog(cog):
	root = getattr(cog, "_attachmentbot_root", None)
	if root:
		root.remove_command(cog.__cog_app_commands_group__.name)
