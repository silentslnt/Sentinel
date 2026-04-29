"""Sticky messages.

When a sticky is set on a channel, the bot re-posts the configured message
after each new (non-bot) message — so the sticky always appears as the latest
message in the channel. Old sticky message is deleted before the new one is sent.

A small per-channel debounce avoids spamming when multiple messages arrive in quick
succession.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import embed_script
from cogs.embeds import fetch_script, build_view

log = logging.getLogger("sentinel.sticky")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sticky_messages (
    guild_id        BIGINT NOT NULL,
    channel_id      BIGINT NOT NULL,
    script          TEXT NOT NULL,
    embed_name      TEXT,            -- if non-null, render this saved embed instead of `script`
    last_message_id BIGINT,
    PRIMARY KEY (guild_id, channel_id)
);
"""

DEBOUNCE_SECONDS = 3.0


class Sticky(commands.Cog):
    """📌 Sticky messages"""

    def __init__(self, bot):
        self.bot = bot
        self._cache: dict[int, dict] = {}  # channel_id -> row
        self._pending: dict[int, asyncio.Task] = {}

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)
        rows = await self.bot.db.fetch("SELECT * FROM sticky_messages")
        self._cache = {r["channel_id"]: dict(r) for r in rows}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        if message.channel.id not in self._cache:
            return
        # Debounce: if a repost is already pending for this channel, let it handle this.
        existing = self._pending.get(message.channel.id)
        if existing and not existing.done():
            return
        self._pending[message.channel.id] = asyncio.create_task(self._repost(message.channel))

    async def _repost(self, channel: discord.TextChannel):
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            row = self._cache.get(channel.id)
            if row is None:
                return

            # Delete prior sticky if we have one.
            if row.get("last_message_id"):
                try:
                    old = await channel.fetch_message(row["last_message_id"])
                    await old.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

            # Render
            if row.get("embed_name"):
                script = await fetch_script(self.bot, row["guild_id"], row["embed_name"])
                view = await build_view(self.bot, row["guild_id"], row["embed_name"])
                if script is None:
                    log.warning("sticky in %s references missing embed %s", channel, row["embed_name"])
                    return
            else:
                script = row["script"]
                view = None

            rendered = embed_script.render(script, user=None, guild=channel.guild, channel=channel)
            try:
                msg = await channel.send(
                    content=rendered.content,
                    embed=rendered.embed,
                    view=view or rendered.view or discord.utils.MISSING,
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning("sticky send failed in %s: %s", channel, e)
                return

            await self.bot.db.execute(
                "UPDATE sticky_messages SET last_message_id=$3 WHERE guild_id=$1 AND channel_id=$2",
                channel.guild.id, channel.id, msg.id,
            )
            row["last_message_id"] = msg.id
        finally:
            self._pending.pop(channel.id, None)

    sticky = app_commands.Group(
        name="sticky",
        description="Sticky messages",
        default_permissions=discord.Permissions(manage_messages=True),
        guild_only=True,
    )

    @sticky.command(name="set", description="Set a sticky message in a channel (text or embed script)")
    @app_commands.describe(
        channel="Channel",
        script="Plain text or embed script",
    )
    async def set_(self, interaction: discord.Interaction, channel: discord.TextChannel, script: str):
        await self.bot.db.execute(
            """INSERT INTO sticky_messages (guild_id, channel_id, script, embed_name, last_message_id)
               VALUES ($1, $2, $3, NULL, NULL)
               ON CONFLICT (guild_id, channel_id) DO UPDATE
               SET script=EXCLUDED.script, embed_name=NULL, last_message_id=NULL""",
            interaction.guild_id, channel.id, script,
        )
        self._cache[channel.id] = {
            "guild_id": interaction.guild_id,
            "channel_id": channel.id,
            "script": script,
            "embed_name": None,
            "last_message_id": None,
        }
        await interaction.response.send_message(f"📌 Sticky set in {channel.mention}.", ephemeral=True)
        # Trigger an immediate post so the user sees it.
        self._pending[channel.id] = asyncio.create_task(self._repost(channel))

    @sticky.command(name="setembed", description="Set a sticky message in a channel using a saved embed")
    async def set_embed(self, interaction: discord.Interaction, channel: discord.TextChannel, embed_name: str):
        if await fetch_script(self.bot, interaction.guild_id, embed_name) is None:
            return await interaction.response.send_message(
                f"❌ No saved embed named `{embed_name}`.", ephemeral=True,
            )
        await self.bot.db.execute(
            """INSERT INTO sticky_messages (guild_id, channel_id, script, embed_name, last_message_id)
               VALUES ($1, $2, '', $3, NULL)
               ON CONFLICT (guild_id, channel_id) DO UPDATE
               SET script='', embed_name=EXCLUDED.embed_name, last_message_id=NULL""",
            interaction.guild_id, channel.id, embed_name,
        )
        self._cache[channel.id] = {
            "guild_id": interaction.guild_id,
            "channel_id": channel.id,
            "script": "",
            "embed_name": embed_name,
            "last_message_id": None,
        }
        await interaction.response.send_message(
            f"📌 Sticky embed `{embed_name}` set in {channel.mention}.", ephemeral=True,
        )
        self._pending[channel.id] = asyncio.create_task(self._repost(channel))

    @sticky.command(name="remove", description="Remove the sticky from a channel")
    async def remove(self, interaction: discord.Interaction, channel: discord.TextChannel):
        row = self._cache.get(channel.id)
        if row is None:
            return await interaction.response.send_message("ℹ️ No sticky in that channel.", ephemeral=True)
        if row.get("last_message_id"):
            try:
                old = await channel.fetch_message(row["last_message_id"])
                await old.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        await self.bot.db.execute(
            "DELETE FROM sticky_messages WHERE guild_id=$1 AND channel_id=$2",
            interaction.guild_id, channel.id,
        )
        self._cache.pop(channel.id, None)
        await interaction.response.send_message(f"✅ Sticky removed from {channel.mention}.", ephemeral=True)

    @sticky.command(name="view", description="Show all stickies in this server")
    async def view(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetch(
            "SELECT channel_id, script, embed_name FROM sticky_messages WHERE guild_id=$1",
            interaction.guild_id,
        )
        if not rows:
            return await interaction.response.send_message("ℹ️ No stickies configured.", ephemeral=True)
        lines = []
        for r in rows:
            ch = interaction.guild.get_channel(r["channel_id"])
            ref = ch.mention if ch else f"<#{r['channel_id']}>"
            if r["embed_name"]:
                lines.append(f"{ref} → embed `{r['embed_name']}`")
            else:
                preview = (r["script"][:60] + "…") if len(r["script"]) > 60 else r["script"]
                lines.append(f"{ref} → `{preview}`")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Sticky(bot))
