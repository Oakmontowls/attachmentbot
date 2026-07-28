import discord


async def send_json_chunks(interaction: discord.Interaction, title: str, content: str):
	chunk_size = 1800
	chunks = [content[i:i + chunk_size] for i in range(0, len(content), chunk_size)] or ["{}"]
	await interaction.response.send_message(
		f"{title}\n```json\n{chunks[0]}\n```",
		ephemeral=True,
	)
	for chunk in chunks[1:]:
		await interaction.followup.send(f"```json\n{chunk}\n```", ephemeral=True)
