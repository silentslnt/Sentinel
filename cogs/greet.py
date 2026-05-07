"""Per-channel greet system with auto-delete and invite tracking variables.

Listens to the custom on_member_join_tracked event dispatched by the invites
cog so inviter info is available without a separate API call.

Commands:
  greet <#channel> <delete_after> <script>   — set greet for a channel
  disablegreet <#channel>                    — remove greet from channel
  greetchannels                              — list all greet channels
  greetvariables                             — list available variables
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from utils import embed_script
from utils.checks import is_guild_admin

log = logging.getLogger("sentinel.greet")

SCHEMA = """
CREATE TABLE IF NOT EXISTS greet_channels (
    guild_id     BIGINT  NOT NULL,
    channel_id   BIGINT  NOT NULL,
    message      TEXT    NOT NULL,
    delete_after INTEGER,
    PRIMARY KEY (guild_id, channel_id)
);
"""

VARIABLES_HELP = """\
**User**
`{user}` / `{user.mention}` — mention
`{user.name}` — username
`{user.id}` — user ID
`{user.tag}` — username#discriminator
`{user.avatar}` — avatar URL
`{user.created_at}` — account age (relative)
`{user.joined_at}` — join time (relative)

**Guild**
`{guild.name}` — server name
`{guild.id}` — server ID
`{guild.count}` — member count
`{guild.icon}` — server icon URL

**Inviter** (requires invite tracking)
`{inviter}` / `{inviter.mention}` — mention
`{inviter.name}` — username
`{inviter.id}` — user ID
`{invite.code}` — invite code used

**Channel**
`{channel.mention}` — channel mention
`{channel.name}` — channel name

**Embed syntax**
`{description: Welcome {user}!}$v{color: ffffff}`
Separate params with `$v`. Use `{author: name && icon}`, `{footer: text}`, `{field: name && value}`, `{image: url}`, `{thumbnail: url}`, `{timestamp}`."""


class Greet(commands.Cog):
    """Per-channel greet messages"""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)

    @commands.Cog.listener()
    async def on_member_join_tracked(
        self,
        member: discord.Member,
        inviter,
        invite_code: str | None,
    ):
        rows = await self.bot.db.fetch(
            "SELECT channel_id, message, delete_after FROM greet_channels WHERE guild_id=$1",
            member.guild.id,
        )
        if not rows:
            return

        for row in rows:
            channel = member.guild.get_channel(row["channel_id"])
            if channel is None:
                continue

            rendered = embed_script.render(
                row["message"],
                user=member,
                guild=member.guild,
                channel=channel,
                inviter=inviter,
                invite_code=invite_code,
            )
            if rendered.is_empty:
                continue

            try:
                msg = await channel.send(
                    content=rendered.content,
                    embed=rendered.embed,
                    view=rendered.view or discord.utils.MISSING,
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning("greet send failed in %s: %s", channel, e)
                continue

            if row["delete_after"]:
                asyncio.create_task(_delete_after(msg, row["delete_after"]))

    # ---- commands ----

    @commands.command(name="greet")
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def greet(self, ctx, channel: discord.TextChannel, delete_after: int, *, message: str):
        """Set a greet message for a channel.

        delete_after: seconds before the greet auto-deletes (0 = never, max 300)
        message: plain text or embed script ({description: ...}$v{color: ...})

        Example:
          .greet #welcome 10 {description: Welcome {user}! Invited by {inviter}.}$v{color: ffffff}
        """
        if delete_after < 0 or delete_after > 300:
            return await ctx.send("delete_after must be 0–300 seconds (0 = never).")

        await self.bot.db.execute(
            """
            INSERT INTO greet_channels (guild_id, channel_id, message, delete_after)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id, channel_id) DO UPDATE
                SET message      = EXCLUDED.message,
                    delete_after = EXCLUDED.delete_after
            """,
            ctx.guild.id,
            channel.id,
            message,
            delete_after or None,
        )
        sd = f" · auto-deletes after {delete_after}s" if delete_after else ""
        await ctx.send(f"Greet set in {channel.mention}{sd}.")

    @commands.command(name="disablegreet")
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def disablegreet(self, ctx, channel: discord.TextChannel):
        """Remove the greet from a channel."""
        result = await self.bot.db.execute(
            "DELETE FROM greet_channels WHERE guild_id=$1 AND channel_id=$2",
            ctx.guild.id, channel.id,
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        if n:
            await ctx.send(f"Greet disabled in {channel.mention}.")
        else:
            await ctx.send(f"No greet was set for {channel.mention}.")

    @commands.command(name="greetchannels")
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def greetchannels(self, ctx):
        """List all channels with greet enabled."""
        rows = await self.bot.db.fetch(
            "SELECT channel_id, delete_after FROM greet_channels WHERE guild_id=$1",
            ctx.guild.id,
        )
        if not rows:
            return await ctx.send("No greet channels configured.")
        lines = []
        for r in rows:
            ch = ctx.guild.get_channel(r["channel_id"])
            label = ch.mention if ch else f"<#{r['channel_id']}>"
            sd = f" · deletes after {r['delete_after']}s" if r["delete_after"] else ""
            lines.append(f"{label}{sd}")
        embed = discord.Embed(
            title="Greet channels",
            description="\n".join(lines),
            color=discord.Color.default(),
        )
        await ctx.send(embed=embed)

    @commands.command(name="greetvariables", aliases=["greetvariable"])
    @commands.guild_only()
    async def greetvariables(self, ctx):
        """Show all variables available in greet messages."""
        embed = discord.Embed(
            title="Greet variables",
            description=VARIABLES_HELP,
            color=discord.Color.default(),
        )
        await ctx.send(embed=embed)


async def _delete_after(msg: discord.Message, seconds: int):
    try:
        await asyncio.sleep(seconds)
        await msg.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


async def setup(bot):
    await bot.add_cog(Greet(bot))
