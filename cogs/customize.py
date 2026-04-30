"""Bot self-customization (avatar / banner). Owner-only, prefix-only."""
from __future__ import annotations

from typing import Optional

import aiohttp
import discord
from discord.ext import commands


class Customize(commands.Cog):
    """🎨 Bot avatar / banner (owner only)"""

    def __init__(self, bot):
        self.bot = bot

    async def _fetch_image(self, url: str) -> Optional[bytes]:
        async with aiohttp.ClientSession() as sess:
            try:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        return None
                    if int(r.headers.get("Content-Length", "0")) > 10 * 1024 * 1024:
                        return None
                    return await r.read()
            except (aiohttp.ClientError, TimeoutError):
                return None

    @commands.group(name="customize", aliases=["cz"], invoke_without_command=True)
    @commands.is_owner()
    async def customize(self, ctx):
        """Customize the bot's profile (owner only)."""
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id if ctx.guild else None)
        await ctx.send(
            f"🎨 **Customize**\n"
            f"`{prefix}customize avatar <url>`\n"
            f"`{prefix}customize banner <url>`",
        )

    @customize.command(name="avatar")
    @commands.is_owner()
    async def avatar(self, ctx, url: str):
        """Change the bot's avatar."""
        async with ctx.typing():
            data = await self._fetch_image(url)
        if data is None:
            return await ctx.send("❌ Couldn't fetch that image.")
        try:
            await self.bot.user.edit(avatar=data)
        except discord.HTTPException as e:
            return await ctx.send(f"❌ {e}")
        await ctx.send("✅ Avatar updated.")

    @customize.command(name="banner")
    @commands.is_owner()
    async def banner(self, ctx, url: str):
        """Change the bot's banner."""
        async with ctx.typing():
            data = await self._fetch_image(url)
        if data is None:
            return await ctx.send("❌ Couldn't fetch that image.")
        try:
            await self.bot.user.edit(banner=data)
        except discord.HTTPException as e:
            return await ctx.send(f"❌ {e}")
        await ctx.send("✅ Banner updated.")


async def setup(bot):
    await bot.add_cog(Customize(bot))
