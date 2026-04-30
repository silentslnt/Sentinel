"""Channel manager — create, delete, rename, set topic / nsfw."""
from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands


CHANNEL_TYPE_CHOICES = [
    app_commands.Choice(name="text", value="text"),
    app_commands.Choice(name="voice", value="voice"),
]


class _ConfirmDelete(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.value: Optional[bool] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the invoker can confirm.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


class ChannelManager(commands.Cog):
    """🪧 Channel manager"""

    def __init__(self, bot):
        self.bot = bot

    channel = app_commands.Group(
        name="channel",
        description="Channel management",
        default_permissions=discord.Permissions(manage_channels=True),
        guild_only=True,
    )

    @channel.command(name="create", description="Create a new channel")
    @app_commands.choices(type=CHANNEL_TYPE_CHOICES)
    @app_commands.describe(name="Channel name", type="text or voice", category="Category to place it under")
    async def create(
        self,
        interaction: discord.Interaction,
        name: str,
        type: Optional[app_commands.Choice[str]] = None,
        category: Optional[discord.CategoryChannel] = None,
    ):
        kind = type.value if type else "text"
        try:
            if kind == "voice":
                ch = await interaction.guild.create_voice_channel(
                    name=name, category=category, reason=f"Created by {interaction.user}",
                )
            else:
                ch = await interaction.guild.create_text_channel(
                    name=name, category=category, reason=f"Created by {interaction.user}",
                )
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I lack permission.", ephemeral=True)
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)
        await interaction.response.send_message(f"✅ Created {ch.mention}.")

    @channel.command(name="delete", description="Delete a channel (with confirmation)")
    async def delete(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel):
        view = _ConfirmDelete(interaction.user.id)
        await interaction.response.send_message(
            f"⚠️ Really delete {channel.mention}? This is irreversible.", view=view, ephemeral=True,
        )
        await view.wait()
        if not view.value:
            return
        try:
            await channel.delete(reason=f"Deleted by {interaction.user}")
        except discord.Forbidden:
            return await interaction.followup.send("❌ I lack permission.", ephemeral=True)
        except discord.HTTPException as e:
            return await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)
        await interaction.followup.send(f"✅ Deleted `#{channel.name}`.", ephemeral=True)

    @channel.command(name="rename", description="Rename a channel")
    async def rename(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel, name: str):
        try:
            await channel.edit(name=name, reason=f"Renamed by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I lack permission.", ephemeral=True)
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)
        await interaction.response.send_message(f"✅ Renamed to {channel.mention}.")

    @channel.command(name="topic", description="Set the topic for a text channel")
    async def topic(self, interaction: discord.Interaction, channel: discord.TextChannel, topic: str):
        try:
            await channel.edit(topic=topic[:1024], reason=f"Topic set by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I lack permission.", ephemeral=True)
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)
        await interaction.response.send_message(f"✅ Topic updated for {channel.mention}.")

    @channel.command(name="nsfw", description="Toggle NSFW flag for a text channel")
    async def nsfw(self, interaction: discord.Interaction, channel: discord.TextChannel):
        try:
            await channel.edit(nsfw=not channel.nsfw, reason=f"NSFW toggled by {interaction.user}")
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)
        await interaction.response.send_message(
            f"✅ {channel.mention} NSFW now **{'on' if not channel.nsfw else 'off'}**.",
        )


async def setup(bot):
    await bot.add_cog(ChannelManager(bot))
