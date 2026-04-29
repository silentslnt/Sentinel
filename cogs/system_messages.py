"""Welcome / Goodbye / Boost system messages with optional auto-delete.

Multiple messages per event are supported (each with its own channel + script).
Each message can self-destruct after N seconds (5–60).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import embed_script

log = logging.getLogger("sentinel.system_messages")

SCHEMA = """
CREATE TABLE IF NOT EXISTS system_messages (
    id            BIGSERIAL PRIMARY KEY,
    guild_id      BIGINT NOT NULL,
    event         TEXT NOT NULL CHECK (event IN ('welcome','goodbye','boost')),
    channel_id    BIGINT NOT NULL,
    script        TEXT NOT NULL,
    self_destruct INTEGER
);

CREATE INDEX IF NOT EXISTS system_messages_lookup
    ON system_messages (guild_id, event);
"""

EVENTS = ("welcome", "goodbye", "boost")
EVENT_CHOICES = [app_commands.Choice(name=e, value=e) for e in EVENTS]


async def _dispatch(bot, event: str, member: discord.Member):
    rows = await bot.db.fetch(
        "SELECT * FROM system_messages WHERE guild_id=$1 AND event=$2",
        member.guild.id,
        event,
    )
    for row in rows:
        channel = member.guild.get_channel(row["channel_id"])
        if channel is None:
            continue
        rendered = embed_script.render(
            row["script"],
            user=member,
            guild=member.guild,
            channel=channel,
        )
        if rendered.is_empty:
            continue
        try:
            msg = await channel.send(
                content=rendered.content,
                embed=rendered.embed,
                view=rendered.view or discord.utils.MISSING,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("system_message send failed in %s: %s", channel, e)
            continue
        if row["self_destruct"]:
            asyncio.create_task(_self_destruct(msg, row["self_destruct"]))


async def _self_destruct(msg: discord.Message, seconds: int):
    try:
        await asyncio.sleep(seconds)
        await msg.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


class SystemMessages(commands.Cog):
    """💬 Welcome / goodbye / boost messages"""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        await _dispatch(self.bot, "welcome", member)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot:
            return
        await _dispatch(self.bot, "goodbye", member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # Booster role gained.
        if before.premium_since is None and after.premium_since is not None:
            await _dispatch(self.bot, "boost", after)

    # ---------------- commands ----------------

    sysmsg = app_commands.Group(
        name="systemmessage",
        description="Configure welcome / goodbye / boost messages",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @sysmsg.command(name="add", description="Add a system message")
    @app_commands.choices(event=EVENT_CHOICES)
    @app_commands.describe(
        event="When to send",
        channel="Where to send",
        script="Embed script (or plain text). Supports {user}, {guild.name}, etc.",
        self_destruct="Delete after N seconds (5-60). Omit to keep forever.",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        event: app_commands.Choice[str],
        channel: discord.TextChannel,
        script: str,
        self_destruct: Optional[app_commands.Range[int, 5, 60]] = None,
    ):
        await self.bot.db.execute(
            "INSERT INTO system_messages (guild_id, event, channel_id, script, self_destruct) "
            "VALUES ($1,$2,$3,$4,$5)",
            interaction.guild_id, event.value, channel.id, script, self_destruct,
        )
        await interaction.response.send_message(
            f"✅ Added a **{event.value}** message in {channel.mention}"
            + (f" (auto-deletes after {self_destruct}s)." if self_destruct else "."),
            ephemeral=True,
        )

    @sysmsg.command(name="remove", description="Remove all system messages of an event in a channel")
    @app_commands.choices(event=EVENT_CHOICES)
    async def remove(
        self,
        interaction: discord.Interaction,
        event: app_commands.Choice[str],
        channel: discord.TextChannel,
    ):
        result = await self.bot.db.execute(
            "DELETE FROM system_messages WHERE guild_id=$1 AND event=$2 AND channel_id=$3",
            interaction.guild_id, event.value, channel.id,
        )
        # asyncpg execute() returns "DELETE N" — strip prefix
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        await interaction.response.send_message(
            f"✅ Removed {n} **{event.value}** message(s) from {channel.mention}.",
            ephemeral=True,
        )

    @sysmsg.command(name="list", description="List all configured system messages")
    async def list_(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetch(
            "SELECT id, event, channel_id, self_destruct, script FROM system_messages "
            "WHERE guild_id=$1 ORDER BY event, id",
            interaction.guild_id,
        )
        if not rows:
            return await interaction.response.send_message("ℹ️ None configured.", ephemeral=True)
        embed = discord.Embed(title="System Messages", color=discord.Color.blurple())
        for r in rows[:25]:
            ch = interaction.guild.get_channel(r["channel_id"])
            preview = (r["script"][:80] + "…") if len(r["script"]) > 80 else r["script"]
            sd = f" · ⏲ {r['self_destruct']}s" if r["self_destruct"] else ""
            embed.add_field(
                name=f"#{r['id']} · {r['event']} · {ch.mention if ch else f'<#{r[\"channel_id\"]}>'}{sd}",
                value=f"`{preview}`",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @sysmsg.command(name="test", description="Trigger a system message as if it just fired")
    @app_commands.choices(event=EVENT_CHOICES)
    async def test(self, interaction: discord.Interaction, event: app_commands.Choice[str]):
        await interaction.response.send_message(f"⏳ Firing **{event.value}**…", ephemeral=True)
        await _dispatch(self.bot, event.value, interaction.user)


async def setup(bot):
    await bot.add_cog(SystemMessages(bot))
