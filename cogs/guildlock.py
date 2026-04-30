"""Home-guild lockdown.

The bot is locked to a single home guild + an explicit whitelist. Any other
server it's added to gets left immediately. On startup, a sweep also leaves any
existing non-permitted guilds (handles the case where someone added the bot
while it was offline).

The home guild is hard-coded as a safety constant — it cannot be removed via
commands. Whitelist additions are persisted in Postgres.
"""
from __future__ import annotations

import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("sentinel.guildlock")

HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", "1490121302386675862"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_whitelist (
    guild_id BIGINT PRIMARY KEY,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class GuildLock(commands.Cog):
    """🔐 Home-guild lockdown"""

    def __init__(self, bot):
        self.bot = bot
        self._allowed: set[int] = {HOME_GUILD_ID}

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)
        rows = await self.bot.db.fetch("SELECT guild_id FROM guild_whitelist")
        self._allowed = {HOME_GUILD_ID} | {r["guild_id"] for r in rows}
        log.info("GuildLock active — allowed guilds: %s", self._allowed)

    def _is_allowed(self, guild_id: int) -> bool:
        return guild_id in self._allowed

    @commands.Cog.listener()
    async def on_ready(self):
        # Startup sweep — leave any guild that isn't allowed.
        for guild in list(self.bot.guilds):
            if self._is_allowed(guild.id):
                continue
            log.warning("Leaving non-permitted guild on startup: %s (%d)", guild.name, guild.id)
            try:
                await guild.leave()
            except discord.HTTPException:
                log.exception("Failed to leave %s", guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        if self._is_allowed(guild.id):
            log.info("Joined permitted guild: %s (%d)", guild.name, guild.id)
            return

        log.warning("Auto-leaving non-permitted guild: %s (%d)", guild.name, guild.id)
        # Best-effort: tell the guild owner we're not allowed here.
        try:
            if guild.owner is not None:
                await guild.owner.send(
                    f"Hi! **Sentinel** is a private bot and isn't authorized for **{guild.name}**. "
                    f"It will leave automatically. If you think this is wrong, reach out to the bot owner."
                )
        except (discord.Forbidden, discord.HTTPException):
            pass
        try:
            await guild.leave()
        except discord.HTTPException:
            log.exception("Failed to leave %s", guild)

    # ----- whitelist commands (owner-only) -----

    whitelist = app_commands.Group(
        name="guildwhitelist",
        description="Manage the guild whitelist (owner only)",
    )

    async def _owner_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.bot.owner_id:
            await interaction.response.send_message("❌ Owner only.", ephemeral=True)
            return False
        return True

    @whitelist.command(name="add", description="Allow the bot to stay in a specific guild")
    @app_commands.describe(guild_id="The guild ID to whitelist")
    async def wl_add(self, interaction: discord.Interaction, guild_id: str):
        if not await self._owner_check(interaction):
            return
        try:
            gid = int(guild_id)
        except ValueError:
            return await interaction.response.send_message("❌ Invalid guild ID.", ephemeral=True)
        await self.bot.db.execute(
            "INSERT INTO guild_whitelist (guild_id) VALUES ($1) ON CONFLICT DO NOTHING", gid,
        )
        self._allowed.add(gid)
        await interaction.response.send_message(f"✅ Whitelisted `{gid}`.", ephemeral=True)

    @whitelist.command(name="remove", description="Remove a guild from the whitelist (does not kick the bot)")
    async def wl_remove(self, interaction: discord.Interaction, guild_id: str):
        if not await self._owner_check(interaction):
            return
        try:
            gid = int(guild_id)
        except ValueError:
            return await interaction.response.send_message("❌ Invalid guild ID.", ephemeral=True)
        if gid == HOME_GUILD_ID:
            return await interaction.response.send_message(
                "❌ Refusing to remove the home guild.", ephemeral=True,
            )
        await self.bot.db.execute("DELETE FROM guild_whitelist WHERE guild_id=$1", gid)
        self._allowed.discard(gid)
        # Note: this doesn't kick the bot from that guild. Owner can /guildwhitelist leave.
        await interaction.response.send_message(
            f"✅ Removed `{gid}` from whitelist. Bot will leave on next restart sweep "
            f"or when re-invited.", ephemeral=True,
        )

    @whitelist.command(name="list", description="Show the current whitelist")
    async def wl_list(self, interaction: discord.Interaction):
        if not await self._owner_check(interaction):
            return
        lines = [f"🏠 Home: `{HOME_GUILD_ID}`"]
        extras = sorted(self._allowed - {HOME_GUILD_ID})
        if extras:
            lines.append("Whitelisted: " + ", ".join(f"`{g}`" for g in extras))
        else:
            lines.append("_No additional whitelisted guilds._")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @whitelist.command(name="leave", description="Force the bot to leave a non-allowed guild now")
    async def wl_leave(self, interaction: discord.Interaction, guild_id: str):
        if not await self._owner_check(interaction):
            return
        try:
            gid = int(guild_id)
        except ValueError:
            return await interaction.response.send_message("❌ Invalid guild ID.", ephemeral=True)
        if gid == HOME_GUILD_ID:
            return await interaction.response.send_message(
                "❌ Refusing to leave the home guild.", ephemeral=True,
            )
        guild = self.bot.get_guild(gid)
        if guild is None:
            return await interaction.response.send_message("❌ I'm not in that guild.", ephemeral=True)
        try:
            await guild.leave()
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)
        await interaction.response.send_message(f"✅ Left `{guild.name}` (`{gid}`).", ephemeral=True)


async def setup(bot):
    await bot.add_cog(GuildLock(bot))
