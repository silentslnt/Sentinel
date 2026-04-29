"""Bot self-customization (avatar / banner / bio). Owner-only."""
from __future__ import annotations

from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands


class Customize(commands.Cog):
    """🎨 Bot avatar / banner / bio (owner only)"""

    def __init__(self, bot):
        self.bot = bot

    customize = app_commands.Group(
        name="customize",
        description="Customize the bot's profile (owner only)",
    )

    async def _owner_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.bot.owner_id:
            await interaction.response.send_message("❌ Owner only.", ephemeral=True)
            return False
        return True

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

    @customize.command(name="avatar", description="Change the bot's avatar")
    @app_commands.describe(url="Direct URL to a PNG/JPG/GIF image")
    async def avatar(self, interaction: discord.Interaction, url: str):
        if not await self._owner_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        data = await self._fetch_image(url)
        if data is None:
            return await interaction.followup.send("❌ Couldn't fetch that image.", ephemeral=True)
        try:
            await self.bot.user.edit(avatar=data)
        except discord.HTTPException as e:
            return await interaction.followup.send(f"❌ {e}", ephemeral=True)
        await interaction.followup.send("✅ Avatar updated.", ephemeral=True)

    @customize.command(name="banner", description="Change the bot's banner")
    async def banner(self, interaction: discord.Interaction, url: str):
        if not await self._owner_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        data = await self._fetch_image(url)
        if data is None:
            return await interaction.followup.send("❌ Couldn't fetch that image.", ephemeral=True)
        try:
            await self.bot.user.edit(banner=data)
        except discord.HTTPException as e:
            return await interaction.followup.send(f"❌ {e}", ephemeral=True)
        await interaction.followup.send("✅ Banner updated.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Customize(bot))
