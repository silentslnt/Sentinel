"""Admin whitelist — lets server owner grant admin-command access to specific users."""
from __future__ import annotations

import discord
from discord.ext import commands

SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_whitelist (
    guild_id BIGINT NOT NULL,
    user_id  BIGINT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
"""


def _owner_only(ctx) -> bool:
    if not ctx.guild:
        raise commands.CheckFailure("This command can only be used in a server.")
    if ctx.author.id != ctx.guild.owner_id:
        raise commands.CheckFailure("Only the server owner can manage the admin whitelist.")
    return True


class Admin(commands.Cog):
    """Admin whitelist management"""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)

    @commands.group(name="admin", invoke_without_command=True)
    @commands.guild_only()
    async def admin(self, ctx):
        """Manage the admin whitelist (server owner only)."""
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        await ctx.send(
            f"`{prefix}admin add <@member>` — whitelist a user\n"
            f"`{prefix}admin remove <@member>` — remove a user\n"
            f"`{prefix}admin list` — show all whitelisted users"
        )

    @admin.command(name="add")
    @commands.guild_only()
    @commands.check(_owner_only)
    async def admin_add(self, ctx, member: discord.Member):
        """Add a user to the admin whitelist."""
        if member.id == ctx.guild.owner_id:
            return await ctx.send("The server owner is always an admin.")
        await self.bot.db.execute(
            "INSERT INTO admin_whitelist (guild_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            ctx.guild.id, member.id,
        )
        await ctx.send(f"{member.mention} added to the admin whitelist.")

    @admin.command(name="remove")
    @commands.guild_only()
    @commands.check(_owner_only)
    async def admin_remove(self, ctx, member: discord.Member):
        """Remove a user from the admin whitelist."""
        result = await self.bot.db.execute(
            "DELETE FROM admin_whitelist WHERE guild_id=$1 AND user_id=$2",
            ctx.guild.id, member.id,
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        if n:
            await ctx.send(f"{member.mention} removed from the admin whitelist.")
        else:
            await ctx.send(f"{member.mention} was not on the whitelist.")

    @admin.command(name="list")
    @commands.guild_only()
    @commands.check(_owner_only)
    async def admin_list(self, ctx):
        """List all whitelisted admins."""
        rows = await self.bot.db.fetch(
            "SELECT user_id FROM admin_whitelist WHERE guild_id=$1", ctx.guild.id
        )
        if not rows:
            return await ctx.send("No users on the admin whitelist.")
        mentions = " ".join(f"<@{r['user_id']}>" for r in rows)
        embed = discord.Embed(title="Admin whitelist", description=mentions, color=discord.Color.default())
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Admin(bot))
