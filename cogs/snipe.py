"""Snipe — show the last deleted or edited message in a channel.

In-memory only (no DB). One slot per channel for delete + one for edit.
Entries expire after 1 hour.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import discord
from discord import app_commands
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
    edited_to: Optional[str] = None  # only for edit-snipes


def _expired(s: Snipe) -> bool:
    return (time.monotonic() - s.timestamp) > EXPIRY_SECONDS


class SnipeCog(commands.Cog):
    """🔍 Snipe deleted / edited messages"""

    def __init__(self, bot):
        self.bot = bot
        # channel_id -> Snipe
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
            when=after.edited_at.timestamp() if after.edited_at else after.created_at.timestamp(),
            edited_to=after.content,
        )

    @app_commands.command(name="snipe", description="Show the last deleted message in this channel")
    @app_commands.guild_only()
    async def snipe(self, interaction: discord.Interaction):
        s = self._deleted.get(interaction.channel_id)
        if s is None or _expired(s):
            self._deleted.pop(interaction.channel_id, None)
            return await interaction.response.send_message(
                "ℹ️ Nothing to snipe in this channel.", ephemeral=True,
            )
        embed = discord.Embed(
            description=s.content or "_no text content_",
            color=discord.Color.dark_red(),
            timestamp=discord.utils.snowflake_time(int(s.when * 1000) << 22) if False else None,
        )
        embed.set_author(name=s.author_name, icon_url=s.author_avatar)
        embed.set_footer(text=f"Deleted from #{interaction.channel.name}")
        if s.attachments:
            embed.add_field(
                name="Attachments",
                value="\n".join(s.attachments[:5]),
                inline=False,
            )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="editsnipe", description="Show the last edited message in this channel")
    @app_commands.guild_only()
    async def editsnipe(self, interaction: discord.Interaction):
        s = self._edited.get(interaction.channel_id)
        if s is None or _expired(s):
            self._edited.pop(interaction.channel_id, None)
            return await interaction.response.send_message(
                "ℹ️ Nothing to snipe in this channel.", ephemeral=True,
            )
        embed = discord.Embed(color=discord.Color.gold())
        embed.set_author(name=s.author_name, icon_url=s.author_avatar)
        embed.add_field(name="Before", value=(s.content or "_empty_")[:1024], inline=False)
        embed.add_field(name="After", value=(s.edited_to or "_empty_")[:1024], inline=False)
        embed.set_footer(text=f"Edited in #{interaction.channel.name}")
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(SnipeCog(bot))
