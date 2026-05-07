"""Per-server bot avatar and banner customization."""
from __future__ import annotations

from typing import Optional

import aiohttp
import discord
from discord.ext import commands

from utils.checks import is_guild_admin


class Customize(commands.Cog):
    """🎨 Server bot appearance"""

    def __init__(self, bot):
        self.bot = bot

    async def _fetch_image(self, url: str) -> Optional[bytes]:
        async with aiohttp.ClientSession() as sess:
            try:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        return None
                    ct = r.headers.get("Content-Type", "")
                    if not any(t in ct for t in ("image/png", "image/jpeg", "image/gif", "image/webp")):
                        # Try to read anyway and let Discord validate
                        pass
                    data = await r.read()
                    if len(data) > 10 * 1024 * 1024:
                        return None
                    return data
            except (aiohttp.ClientError, TimeoutError):
                return None

    @commands.group(name="customize", aliases=["cz"], invoke_without_command=True)
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def customize(self, ctx):
        """Customize the bot's appearance in this server."""
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        await ctx.send(
            f"`{prefix}customize avatar <url>` — set bot avatar for this server\n"
            f"`{prefix}customize banner <url>` — set bot banner for this server\n"
            f"`{prefix}customize resetavatar` — remove server avatar override\n"
            f"`{prefix}customize resetbanner` — remove server banner override",
        )

    @customize.command(name="avatar")
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def set_avatar(self, ctx, url: str):
        """Set the bot's avatar for this server."""
        async with ctx.typing():
            data = await self._fetch_image(url)
        if data is None:
            return await ctx.send("❌ Couldn't fetch that image. Make sure it's a direct image URL under 10MB.")
        try:
            await ctx.guild.me.edit(avatar=data)
        except discord.HTTPException as e:
            return await ctx.send(f"❌ {e}")
        await ctx.send("✅ Server avatar updated.")

    @customize.command(name="banner")
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def set_banner(self, ctx, url: str):
        """Set the bot's banner for this server."""
        async with ctx.typing():
            data = await self._fetch_image(url)
        if data is None:
            return await ctx.send("❌ Couldn't fetch that image. Make sure it's a direct image URL under 10MB.")
        try:
            await ctx.guild.me.edit(banner=data)
        except discord.HTTPException as e:
            return await ctx.send(f"❌ {e}")
        await ctx.send("✅ Server banner updated.")

    @customize.command(name="resetavatar")
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def reset_avatar(self, ctx):
        """Remove this server's bot avatar override."""
        try:
            await ctx.guild.me.edit(avatar=None)
        except discord.HTTPException as e:
            return await ctx.send(f"❌ {e}")
        await ctx.send("✅ Server avatar reset to default.")

    @customize.command(name="resetbanner")
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def reset_banner(self, ctx):
        """Remove this server's bot banner override."""
        try:
            await ctx.guild.me.edit(banner=None)
        except discord.HTTPException as e:
            return await ctx.send(f"❌ {e}")
        await ctx.send("✅ Server banner reset to default.")


async def setup(bot):
    await bot.add_cog(Customize(bot))
