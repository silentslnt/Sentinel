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


def with_perms(**perms):
    """Drop-in for has_permissions that also honours fake permissions from the fp system."""
    async def predicate(ctx):
        if not ctx.guild:
            raise commands.NoPrivateMessage()
        real = ctx.author.guild_permissions
        missing = [p for p, v in perms.items() if getattr(real, p, None) != v]
        if not missing:
            return True
        # Check fake permissions via the Restrictions cog's in-memory cache.
        restrictions_cog = ctx.bot.cogs.get("Restrictions")
        role_ids = {r.id for r in ctx.author.roles}
        still_missing = []
        for perm in missing:
            has_fake = False
            if restrictions_cog:
                guild_fps = restrictions_cog._fake_perms.get(ctx.guild.id, {})
                has_fake = any(perm in guild_fps.get(rid, set()) for rid in role_ids)
            if not has_fake:
                still_missing.append(perm)
        if still_missing:
            raise commands.MissingPermissions(still_missing)
        return True
    return commands.check(predicate)


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
