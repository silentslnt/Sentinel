"""Snipe — deleted and edited message history per channel.

Stores up to 10 deleted and 10 edited messages per channel (in-memory, 1hr expiry).

Commands:
  snipe [index]      — show Nth last deleted message (aliases: s, s2-s5)
  editsnipe [index]  — show Nth last edited message  (aliases: es, es2-es5)
  clearsnipe         — clear snipe history for this channel (aliases: cs)
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import discord
from discord.ext import commands

from utils.checks import with_perms

EXPIRY_SECONDS = 3600
MAX_HISTORY = 10


@dataclass
class Snipe:
    author_id: int
    author_name: str
    author_avatar: str
    content: str
    attachments: list[str]
    timestamp: float   # monotonic — for expiry
    when: float        # epoch — for display
    edited_to: Optional[str] = None


def _expired(s: Snipe) -> bool:
    return (time.monotonic() - s.timestamp) > EXPIRY_SECONDS


def _get(history: deque[Snipe], index: int) -> Optional[Snipe]:
    """Return the Nth entry (1-based) pruning expired ones first."""
    while history and _expired(history[-1]):
        history.pop()
    if not history or index < 1 or index > len(history):
        return None
    return history[index - 1]


def _index_from_invocation(invoked_with: str, base: str) -> int:
    """Derive index from alias name — e.g. 's3' -> 3, 'es2' -> 2."""
    suffix = invoked_with[len(base):]
    return int(suffix) if suffix.isdigit() else 1


class SnipeCog(commands.Cog):
    """Snipe deleted / edited messages"""

    def __init__(self, bot):
        self.bot = bot
        # channel_id -> deque[Snipe] (index 0 = most recent)
        self._deleted: dict[int, deque[Snipe]] = {}
        self._edited: dict[int, deque[Snipe]] = {}

    # ---- listeners ----

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        if not (message.content or message.attachments):
            return
        dq = self._deleted.setdefault(message.channel.id, deque(maxlen=MAX_HISTORY))
        dq.appendleft(Snipe(
            author_id=message.author.id,
            author_name=str(message.author),
            author_avatar=message.author.display_avatar.url,
            content=message.content,
            attachments=[a.url for a in message.attachments],
            timestamp=time.monotonic(),
            when=message.created_at.timestamp(),
        ))

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.guild is None or after.author.bot or before.content == after.content:
            return
        dq = self._edited.setdefault(after.channel.id, deque(maxlen=MAX_HISTORY))
        dq.appendleft(Snipe(
            author_id=after.author.id,
            author_name=str(after.author),
            author_avatar=after.author.display_avatar.url,
            content=before.content,
            attachments=[],
            timestamp=time.monotonic(),
            when=(after.edited_at or after.created_at).timestamp(),
            edited_to=after.content,
        ))

    # ---- snipe ----

    @commands.hybrid_command(name="snipe", aliases=["s", "s2", "s3", "s4", "s5"])
    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def snipe(self, ctx, index: int = None):
        """Show a deleted message. Use s2, s3... for older ones."""
        if index is None:
            index = _index_from_invocation(ctx.invoked_with or "s", "s")
        dq = self._deleted.get(ctx.channel.id, deque())
        s = _get(dq, index)
        if s is None:
            return await ctx.send(
                f"Nothing to snipe at position {index}." if index > 1
                else "Nothing to snipe in this channel."
            )
        total = sum(1 for e in dq if not _expired(e))
        embed = discord.Embed(
            description=s.content or "_no text_",
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=s.author_name, icon_url=s.author_avatar)
        embed.set_footer(text=f"#{index} of {total} · deleted from #{ctx.channel.name}")
        if s.attachments:
            embed.add_field(name="Attachments", value="\n".join(s.attachments[:5]), inline=False)
        await ctx.send(embed=embed)

    # ---- editsnipe ----

    @commands.hybrid_command(name="editsnipe", aliases=["es", "es2", "es3", "es4", "es5"])
    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def editsnipe(self, ctx, index: int = None):
        """Show an edited message. Use es2, es3... for older ones."""
        if index is None:
            index = _index_from_invocation(ctx.invoked_with or "es", "es")
        dq = self._edited.get(ctx.channel.id, deque())
        s = _get(dq, index)
        if s is None:
            return await ctx.send(
                f"Nothing to editsnipe at position {index}." if index > 1
                else "Nothing to editsnipe in this channel."
            )
        total = sum(1 for e in dq if not _expired(e))
        embed = discord.Embed(color=discord.Color.gold(), timestamp=discord.utils.utcnow())
        embed.set_author(name=s.author_name, icon_url=s.author_avatar)
        embed.add_field(name="Before", value=(s.content or "_empty_")[:1024], inline=False)
        embed.add_field(name="After", value=(s.edited_to or "_empty_")[:1024], inline=False)
        embed.set_footer(text=f"#{index} of {total} · edited in #{ctx.channel.name}")
        await ctx.send(embed=embed)

    # ---- clearsnipe ----

    @commands.command(name="clearsnipe", aliases=["cs"])
    @commands.guild_only()
    @with_perms(manage_messages=True)
    async def clearsnipe(self, ctx):
        """Clear all snipe history for this channel."""
        self._deleted.pop(ctx.channel.id, None)
        self._edited.pop(ctx.channel.id, None)
        try:
            await ctx.message.add_reaction("✅")
        except (discord.Forbidden, discord.HTTPException):
            pass


async def setup(bot):
    await bot.add_cog(SnipeCog(bot))
