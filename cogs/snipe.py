"""Snipe — show the last deleted or edited message in a channel.

In-memory only (no DB). One slot per channel for delete + one for edit.
Entries expire after 1 hour.

Hybrid commands so both `/snipe` and `.snipe` (or `.s`) work.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import discord
from discord.ext import commands

EXPIRY_SECONDS = 3600


@dataclass
class Snipe:
    author_id: int
    author_name: str
    author_avatar: str
    content: str
    attachments: list[str]
    timestamp: float  # monotonic
    when: float       # epoch seconds for display
    edited_to: Optional[str] = None


def _expired(s: Snipe) -> bool:
    return (time.monotonic() - s.timestamp) > EXPIRY_SECONDS


class SnipeCog(commands.Cog):
    """🔍 Snipe deleted / edited messages"""

    def __init__(self, bot):
        self.bot = bot
        self._deleted: dict[int, Snipe] = {}
        self._edited: dict[int, Snipe] = {}

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        if not (message.content or message.attachments):
            return
        self._deleted[message.channel.id] = Snipe(
            author_id=message.author.id,
            author_name=str(message.author),
            author_avatar=message.author.display_avatar.url,
            content=message.content,
            attachments=[a.url for a in message.attachments],
            timestamp=time.monotonic(),
            when=message.created_at.timestamp(),
        )

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.guild is None or after.author.bot or before.content == after.content:
            return
        self._edited[after.channel.id] = Snipe(
            author_id=after.author.id,
            author_name=str(after.author),
            author_avatar=after.author.display_avatar.url,
            content=before.content,
            attachments=[],
            timestamp=time.monotonic(),
            when=(after.edited_at or after.created_at).timestamp(),
            edited_to=after.content,
        )

    @commands.hybrid_command(name="snipe", aliases=["s"])
    @commands.guild_only()
    async def snipe(self, ctx):
        """Show the last deleted message in this channel."""
        s = self._deleted.get(ctx.channel.id)
        if s is None or _expired(s):
            self._deleted.pop(ctx.channel.id, None)
            return await ctx.send("ℹ️ Nothing to snipe in this channel.")
        embed = discord.Embed(
            description=s.content or "_no text content_",
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=s.author_name, icon_url=s.author_avatar)
        embed.set_footer(text=f"Deleted from #{ctx.channel.name}")
        if s.attachments:
            embed.add_field(
                name="Attachments",
                value="\n".join(s.attachments[:5]),
                inline=False,
            )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="editsnipe", aliases=["es"])
    @commands.guild_only()
    async def editsnipe(self, ctx):
        """Show the last edited message in this channel."""
        s = self._edited.get(ctx.channel.id)
        if s is None or _expired(s):
            self._edited.pop(ctx.channel.id, None)
            return await ctx.send("ℹ️ Nothing to snipe in this channel.")
        embed = discord.Embed(color=discord.Color.gold(), timestamp=discord.utils.utcnow())
        embed.set_author(name=s.author_name, icon_url=s.author_avatar)
        embed.add_field(name="Before", value=(s.content or "_empty_")[:1024], inline=False)
        embed.add_field(name="After", value=(s.edited_to or "_empty_")[:1024], inline=False)
        embed.set_footer(text=f"Edited in #{ctx.channel.name}")
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(SnipeCog(bot))
