"""Shared command checks."""
from __future__ import annotations

from discord.ext import commands


async def is_guild_admin(ctx) -> bool:
    """Pass if user is server owner, has administrator, or is on the admin whitelist."""
    if not ctx.guild:
        raise commands.CheckFailure("This command can only be used in a server.")
    if ctx.author.id == ctx.guild.owner_id:
        return True
    if ctx.author.guild_permissions.administrator:
        return True
    result = await ctx.bot.db.fetchval(
        "SELECT 1 FROM admin_whitelist WHERE guild_id=$1 AND user_id=$2",
        ctx.guild.id, ctx.author.id,
    )
    if result:
        return True
    raise commands.CheckFailure("You need administrator permission or be on the admin whitelist.")


async def is_whitelisted(ctx) -> bool:
    """Stricter check — only server owner, bot owner, or explicitly whitelisted users.
    Administrator permission alone is NOT enough."""
    if not ctx.guild:
        raise commands.CheckFailure("This command can only be used in a server.")
    if ctx.author.id == ctx.guild.owner_id:
        return True
    if ctx.author.id == ctx.bot.owner_id:
        return True
    result = await ctx.bot.db.fetchval(
        "SELECT 1 FROM admin_whitelist WHERE guild_id=$1 AND user_id=$2",
        ctx.guild.id, ctx.author.id,
    )
    if result:
        return True
    raise commands.CheckFailure("This command is restricted to whitelisted users only.")
