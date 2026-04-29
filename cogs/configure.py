"""Per-guild configuration commands. Currently: prefix."""
from __future__ import annotations

import discord
from discord.ext import commands

MAX_PREFIX_LEN = 5


class Configure(commands.Cog):
    """⚙️ Per-guild configuration"""

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_group(name="configure", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def configure(self, ctx):
        """Configure server-specific settings."""
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        embed = discord.Embed(
            title="⚙️ Server Configuration",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Prefix", value=f"`{prefix}`", inline=True)
        embed.set_footer(text=f"Use {prefix}configure prefix <new> to change the prefix")
        await ctx.send(embed=embed)

    @configure.command(name="prefix")
    @commands.has_permissions(manage_guild=True)
    async def configure_prefix(self, ctx, new_prefix: str):
        """Change this server's command prefix (max 5 chars)."""
        if len(new_prefix) > MAX_PREFIX_LEN:
            return await ctx.send(f"❌ Prefix must be at most {MAX_PREFIX_LEN} characters.")
        if any(c.isspace() for c in new_prefix):
            return await ctx.send("❌ Prefix can't contain whitespace.")

        await self.bot.guild_config.set_prefix(ctx.guild.id, new_prefix)
        await ctx.send(f"✅ Prefix updated to `{new_prefix}`")

    @configure.command(name="resetprefix")
    @commands.has_permissions(manage_guild=True)
    async def configure_resetprefix(self, ctx):
        """Reset the prefix to the bot default."""
        await self.bot.guild_config.reset_prefix(ctx.guild.id)
        await ctx.send(f"✅ Prefix reset to `{self.bot.guild_config.default_prefix}`")


async def setup(bot):
    await bot.add_cog(Configure(bot))
