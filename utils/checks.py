from discord.ext import commands

def is_owner():
    """Check if user is bot owner"""
    async def predicate(ctx):
        return ctx.author.id == ctx.bot.owner_id
    return commands.check(predicate)

def is_admin():
    """Check if user has admin permissions"""
    async def predicate(ctx):
        return ctx.author.guild_permissions.administrator
    return commands.check(predicate)

def is_mod():
    """Check if user has moderator permissions"""
    async def predicate(ctx):
        perms = ctx.author.guild_permissions
        return perms.kick_members or perms.ban_members or perms.manage_messages
    return commands.check(predicate)