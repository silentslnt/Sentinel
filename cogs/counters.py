"""Counter channels.

Update voice/text channel names with live server stats. Discord rate-limits
channel renames to 2 per 10 minutes per channel — this cog updates every
10 minutes to stay safely under that.

Counter types:
  members, humans, bots, boosts, channels, online

Prefix-only commands.
"""
from __future__ import annotations

import logging
from typing import Callable

import discord
from discord.ext import commands, tasks

log = logging.getLogger("sentinel.counters")

SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (
    channel_id BIGINT PRIMARY KEY,
    guild_id   BIGINT NOT NULL,
    type       TEXT NOT NULL,
    template   TEXT NOT NULL DEFAULT '{type}: {value}'
);

CREATE INDEX IF NOT EXISTS counters_guild ON counters (guild_id);
"""

COUNTER_FNS: dict[str, Callable[[discord.Guild], int]] = {
    "members":  lambda g: g.member_count or 0,
    "humans":   lambda g: sum(1 for m in g.members if not m.bot),
    "bots":     lambda g: sum(1 for m in g.members if m.bot),
    "boosts":   lambda g: g.premium_subscription_count or 0,
    "channels": lambda g: len(g.channels),
    "online":   lambda g: sum(1 for m in g.members if m.status is not discord.Status.offline),
}


class Counters(commands.Cog):
    """🔢 Counter channels"""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)
        self.update_loop.start()

    async def cog_unload(self):
        self.update_loop.cancel()

    @tasks.loop(minutes=10)
    async def update_loop(self):
        rows = await self.bot.db.fetch("SELECT * FROM counters")
        for r in rows:
            guild = self.bot.get_guild(r["guild_id"])
            if guild is None:
                continue
            channel = guild.get_channel(r["channel_id"])
            if channel is None:
                await self.bot.db.execute("DELETE FROM counters WHERE channel_id=$1", r["channel_id"])
                continue
            fn = COUNTER_FNS.get(r["type"])
            if fn is None:
                continue
            try:
                value = fn(guild)
            except Exception:
                log.exception("counter fn failed for %s", r["type"])
                continue
            new_name = r["template"].replace("{type}", r["type"]).replace("{value}", str(value))
            if channel.name == new_name:
                continue
            try:
                await channel.edit(name=new_name[:100], reason="Counter update")
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning("counter rename failed for %s: %s", channel, e)

    @update_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ---------------- commands ----------------

    @commands.group(name="counter", aliases=["ct"], invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    async def counter(self, ctx):
        """Counter channels. Subcommands: add, remove, list."""
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        types = ", ".join(f"`{t}`" for t in COUNTER_FNS)
        await ctx.send(
            f"🔢 **Counters** — types: {types}\n"
            f"`{prefix}counter add <#voice_channel> <type> [template]`\n"
            f"`{prefix}counter remove <#voice_channel>`\n"
            f"`{prefix}counter list`",
        )

    @counter.command(name="add")
    async def add(self, ctx, channel: discord.VoiceChannel, type: str, *, template: str = "{type}: {value}"):
        """Turn an existing voice channel into a counter."""
        type = type.lower()
        if type not in COUNTER_FNS:
            return await ctx.send(f"❌ Unknown type. Choose: {', '.join(COUNTER_FNS)}")
        await self.bot.db.execute(
            """INSERT INTO counters (channel_id, guild_id, type, template) VALUES ($1, $2, $3, $4)
               ON CONFLICT (channel_id) DO UPDATE SET type=EXCLUDED.type, template=EXCLUDED.template""",
            channel.id, ctx.guild.id, type, template,
        )
        await self.update_loop.coro(self)
        await ctx.send(f"✅ {channel.mention} is now a `{type}` counter (updates every 10m).")

    @counter.command(name="remove")
    async def remove(self, ctx, channel: discord.VoiceChannel):
        """Stop using a channel as a counter."""
        result = await self.bot.db.execute(
            "DELETE FROM counters WHERE channel_id=$1", channel.id,
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        if n == 0:
            return await ctx.send("ℹ️ That channel isn't a counter.")
        await ctx.send(f"✅ {channel.mention} is no longer a counter.")

    @counter.command(name="list")
    async def list_(self, ctx):
        """List counter channels in this server."""
        rows = await self.bot.db.fetch(
            "SELECT channel_id, type, template FROM counters WHERE guild_id=$1",
            ctx.guild.id,
        )
        if not rows:
            return await ctx.send("ℹ️ No counters configured.")
        lines = []
        for r in rows:
            ch = ctx.guild.get_channel(r["channel_id"])
            ref = ch.mention if ch else f"<#{r['channel_id']}>"
            lines.append(f"{ref} → `{r['type']}` (`{r['template']}`)")
        await ctx.send("\n".join(lines))


async def setup(bot):
    await bot.add_cog(Counters(bot))
