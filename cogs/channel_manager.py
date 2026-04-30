"""Channel manager — create, delete, rename, set topic / nsfw. Prefix-only."""
from __future__ import annotations

from typing import Optional

import discord
from discord.ext import commands


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

    @commands.group(name="channel", aliases=["ch"], invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    async def channel(self, ctx):
        """Channel management."""
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        await ctx.send(
            f"🪧 **Channel manager**\n"
            f"`{prefix}channel create <name> [text|voice] [#category]`\n"
            f"`{prefix}channel delete <#channel>`\n"
            f"`{prefix}channel rename <#channel> <name>`\n"
            f"`{prefix}channel topic <#channel> <topic>`\n"
            f"`{prefix}channel nsfw <#channel>`",
        )

    @channel.command(name="create")
    async def create(self, ctx, name: str, kind: Optional[str] = "text", category: Optional[discord.CategoryChannel] = None):
        """Create a new channel."""
        kind = (kind or "text").lower()
        if kind not in ("text", "voice"):
            return await ctx.send("❌ Type must be `text` or `voice`.")
        try:
            if kind == "voice":
                ch = await ctx.guild.create_voice_channel(
                    name=name, category=category, reason=f"Created by {ctx.author}",
                )
            else:
                ch = await ctx.guild.create_text_channel(
                    name=name, category=category, reason=f"Created by {ctx.author}",
                )
        except discord.Forbidden:
            return await ctx.send("❌ I lack permission.")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Failed: {e}")
        await ctx.send(f"✅ Created {ch.mention}.")

    @channel.command(name="delete")
    async def delete(self, ctx, channel: discord.abc.GuildChannel):
        """Delete a channel (with confirmation)."""
        view = _ConfirmDelete(ctx.author.id)
        prompt = await ctx.send(
            f"⚠️ Really delete {channel.mention}? This is irreversible.", view=view,
        )
        await view.wait()
        if not view.value:
            try:
                await prompt.edit(content="❌ Delete cancelled.", view=None)
            except discord.HTTPException:
                pass
            return
        try:
            await channel.delete(reason=f"Deleted by {ctx.author}")
        except discord.Forbidden:
            return await ctx.send("❌ I lack permission.")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Failed: {e}")
        await ctx.send(f"✅ Deleted `#{channel.name}`.")

    @channel.command(name="rename")
    async def rename(self, ctx, channel: discord.abc.GuildChannel, *, name: str):
        """Rename a channel."""
        try:
            await channel.edit(name=name, reason=f"Renamed by {ctx.author}")
        except discord.Forbidden:
            return await ctx.send("❌ I lack permission.")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Failed: {e}")
        await ctx.send(f"✅ Renamed to {channel.mention}.")

    @channel.command(name="topic")
    async def topic(self, ctx, channel: discord.TextChannel, *, topic: str):
        """Set the topic for a text channel."""
        try:
            await channel.edit(topic=topic[:1024], reason=f"Topic set by {ctx.author}")
        except discord.Forbidden:
            return await ctx.send("❌ I lack permission.")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Failed: {e}")
        await ctx.send(f"✅ Topic updated for {channel.mention}.")

    @channel.command(name="nsfw")
    async def nsfw(self, ctx, channel: discord.TextChannel):
        """Toggle NSFW flag for a text channel."""
        try:
            await channel.edit(nsfw=not channel.nsfw, reason=f"NSFW toggled by {ctx.author}")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Failed: {e}")
        await ctx.send(
            f"✅ {channel.mention} NSFW now **{'on' if not channel.nsfw else 'off'}**.",
        )


async def setup(bot):
    await bot.add_cog(ChannelManager(bot))
