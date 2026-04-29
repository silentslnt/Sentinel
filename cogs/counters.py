"""Counter channels.

Update voice/text channel names with live server stats. Discord rate-limits
channel renames to 2 per 10 minutes per channel — this cog updates every
10 minutes to stay safely under that.

Counter types:
  members       — total members
  humans        — non-bot members
  bots          — bot members
  boosts        — boost count
  channels      — total channels
  online        — members with non-offline status (requires presence intent)
"""
from __future__ import annotations

import logging
from typing import Callable

import discord
from discord import app_commands
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

COUNTER_CHOICES = [app_commands.Choice(name=k, value=k) for k in COUNTER_FNS]


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
                # Channel deleted; clean up.
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

    counter = app_commands.Group(
        name="counter",
        description="Counter channels",
        default_permissions=discord.Permissions(manage_channels=True),
        guild_only=True,
    )

    @counter.command(name="add", description="Turn an existing voice/text channel into a counter")
    @app_commands.choices(type=COUNTER_CHOICES)
    @app_commands.describe(
        channel="Channel whose name will be auto-updated",
        type="What to count",
        template="Display template ({type} and {value} are substituted)",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
        type: app_commands.Choice[str],
        template: str = "{type}: {value}",
    ):
        await self.bot.db.execute(
            """INSERT INTO counters (channel_id, guild_id, type, template) VALUES ($1, $2, $3, $4)
               ON CONFLICT (channel_id) DO UPDATE SET type=EXCLUDED.type, template=EXCLUDED.template""",
            channel.id, interaction.guild_id, type.value, template,
        )
        # Run an immediate update.
        await self.update_loop.coro(self)
        await interaction.response.send_message(
            f"✅ {channel.mention} is now a `{type.value}` counter (updates every 10m).", ephemeral=True,
        )

    @counter.command(name="remove", description="Stop using a channel as a counter")
    async def remove(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        result = await self.bot.db.execute(
            "DELETE FROM counters WHERE channel_id=$1", channel.id,
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        if n == 0:
            return await interaction.response.send_message("ℹ️ That channel isn't a counter.", ephemeral=True)
        await interaction.response.send_message(
            f"✅ {channel.mention} is no longer a counter (channel itself wasn't deleted).", ephemeral=True,
        )

    @counter.command(name="list", description="List counter channels in this server")
    async def list_(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetch(
            "SELECT channel_id, type, template FROM counters WHERE guild_id=$1",
            interaction.guild_id,
        )
        if not rows:
            return await interaction.response.send_message("ℹ️ No counters configured.", ephemeral=True)
        lines = []
        for r in rows:
            ch = interaction.guild.get_channel(r["channel_id"])
            lines.append(f"{ch.mention if ch else f'<#{r[\"channel_id\"]}>'} → `{r['type']}` (`{r['template']}`)")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Counters(bot))
