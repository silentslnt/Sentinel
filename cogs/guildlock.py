"""Home-guild lockdown.

The bot is locked to a single home guild + an explicit whitelist. Any other
server it's added to gets left immediately. On startup, a sweep also leaves any
existing non-permitted guilds.

The home guild is hard-coded as a safety constant — it cannot be removed via
commands. Whitelist additions are persisted in Postgres.

Commands are prefix-only and owner-only.
"""
from __future__ import annotations

import logging
import os

import discord
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

    # ---------------- prefix commands (owner-only) ----------------

    @commands.group(name="guildwhitelist", aliases=["gwl"], invoke_without_command=True)
    @commands.is_owner()
    async def whitelist(self, ctx):
        """Manage the guild whitelist (owner only)."""
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id if ctx.guild else None)
        await ctx.send(
            f"🔐 **Guild whitelist**\n"
            f"`{prefix}guildwhitelist add <guild_id>`\n"
            f"`{prefix}guildwhitelist remove <guild_id>`\n"
            f"`{prefix}guildwhitelist list`\n"
            f"`{prefix}guildwhitelist leave <guild_id>`",
        )

    @whitelist.command(name="add")
    @commands.is_owner()
    async def wl_add(self, ctx, guild_id: int):
        """Allow the bot to stay in a specific guild."""
        await self.bot.db.execute(
            "INSERT INTO guild_whitelist (guild_id) VALUES ($1) ON CONFLICT DO NOTHING", guild_id,
        )
        self._allowed.add(guild_id)
        await ctx.send(f"✅ Whitelisted `{guild_id}`.")

    @whitelist.command(name="remove")
    @commands.is_owner()
    async def wl_remove(self, ctx, guild_id: int):
        """Remove a guild from the whitelist (does not kick the bot)."""
        if guild_id == HOME_GUILD_ID:
            return await ctx.send("❌ Refusing to remove the home guild.")
        await self.bot.db.execute("DELETE FROM guild_whitelist WHERE guild_id=$1", guild_id)
        self._allowed.discard(guild_id)
        await ctx.send(
            f"✅ Removed `{guild_id}` from whitelist. Bot will leave on next restart sweep "
            f"or when re-invited.",
        )

    @whitelist.command(name="list")
    @commands.is_owner()
    async def wl_list(self, ctx):
        """Show the current whitelist."""
        def _guild_label(gid: int) -> str:
            g = self.bot.get_guild(gid)
            return f"**{g.name}** (`{gid}`)" if g else f"`{gid}`"

        lines = [f"Home: {_guild_label(HOME_GUILD_ID)}"]
        extras = sorted(self._allowed - {HOME_GUILD_ID})
        if extras:
            lines.append("Whitelisted:")
            lines.extend(f"• {_guild_label(gid)}" for gid in extras)
        else:
            lines.append("_No additional whitelisted guilds._")
        await ctx.send("\n".join(lines))

    @whitelist.command(name="leave")
    @commands.is_owner()
    async def wl_leave(self, ctx, guild_id: int):
        """Force the bot to leave a non-allowed guild now."""
        if guild_id == HOME_GUILD_ID:
            return await ctx.send("❌ Refusing to leave the home guild.")
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return await ctx.send("❌ I'm not in that guild.")
        try:
            await guild.leave()
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Failed: {e}")
        await ctx.send(f"✅ Left `{guild.name}` (`{guild_id}`).")


async def setup(bot):
    await bot.add_cog(GuildLock(bot))
