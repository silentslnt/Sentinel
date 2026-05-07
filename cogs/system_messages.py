"""Welcome / Goodbye / Boost system messages with optional auto-delete.

Multiple messages per event are supported (each with its own channel + script).
Each message can self-destruct after N seconds (5–60).

Prefix-only commands.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord.ext import commands

from utils import embed_script
from utils.checks import is_guild_admin

log = logging.getLogger("sentinel.system_messages")

SCHEMA = """
CREATE TABLE IF NOT EXISTS system_messages (
    id            BIGSERIAL PRIMARY KEY,
    guild_id      BIGINT NOT NULL,
    event         TEXT NOT NULL CHECK (event IN ('welcome','goodbye','boost')),
    channel_id    BIGINT NOT NULL,
    script        TEXT NOT NULL,
    self_destruct INTEGER
);

CREATE INDEX IF NOT EXISTS system_messages_lookup
    ON system_messages (guild_id, event);
"""

EVENTS = ("welcome", "goodbye", "boost")


async def _dispatch(bot, event: str, member: discord.Member, inviter=None, invite_code=None):
    rows = await bot.db.fetch(
        "SELECT * FROM system_messages WHERE guild_id=$1 AND event=$2",
        member.guild.id, event,
    )
    for row in rows:
        channel = member.guild.get_channel(row["channel_id"])
        if channel is None:
            continue
        rendered = embed_script.render(
            row["script"], user=member, guild=member.guild, channel=channel,
            inviter=inviter, invite_code=invite_code,
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
            log.warning("system_message send failed in %s: %s", channel, e)
            continue
        if row["self_destruct"]:
            asyncio.create_task(_self_destruct(msg, row["self_destruct"]))


async def _self_destruct(msg: discord.Message, seconds: int):
    try:
        await asyncio.sleep(seconds)
        await msg.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


class SystemMessages(commands.Cog):
    """💬 Welcome / goodbye / boost messages"""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)

    @commands.Cog.listener()
    async def on_member_join_tracked(self, member: discord.Member, inviter, invite_code):
        if member.bot:
            return
        await _dispatch(self.bot, "welcome", member, inviter=inviter, invite_code=invite_code)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot:
            return
        await _dispatch(self.bot, "goodbye", member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.premium_since is None and after.premium_since is not None:
            await _dispatch(self.bot, "boost", after)

    # ---------------- commands ----------------

    @commands.group(name="systemmessage", aliases=["sysmsg"], invoke_without_command=True)
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def sysmsg(self, ctx):
        """Welcome / goodbye / boost messages."""
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        await ctx.send(
            f"💬 **System messages** — events: `welcome`, `goodbye`, `boost`\n"
            f"`{prefix}systemmessage add <event> <#channel> [self_destruct=N] | <script>`\n"
            f"`{prefix}systemmessage remove <event> <#channel>`\n"
            f"`{prefix}systemmessage list`\n"
            f"`{prefix}systemmessage test <event>` — preview here",
        )

    @sysmsg.command(name="add")
    async def add(self, ctx, event: str, channel: discord.TextChannel, *, body: str):
        """Add a system message. body format: [self_destruct=N] | <script>"""
        event = event.lower()
        if event not in EVENTS:
            return await ctx.send(f"❌ Event must be one of: {', '.join(EVENTS)}")

        self_destruct: Optional[int] = None
        if "|" in body:
            head, script = body.split("|", 1)
            head = head.strip()
            script = script.strip()
            if head.lower().startswith("self_destruct="):
                try:
                    self_destruct = int(head.split("=", 1)[1])
                except ValueError:
                    return await ctx.send("❌ self_destruct must be an integer 5–60.")
                if self_destruct < 5 or self_destruct > 60:
                    return await ctx.send("❌ self_destruct must be 5–60 seconds.")
            elif head:
                return await ctx.send("❌ Unknown option before `|`. Only `self_destruct=N` is supported.")
        else:
            script = body.strip()

        if not script:
            return await ctx.send("❌ Script can't be empty.")

        await self.bot.db.execute(
            "INSERT INTO system_messages (guild_id, event, channel_id, script, self_destruct) "
            "VALUES ($1,$2,$3,$4,$5)",
            ctx.guild.id, event, channel.id, script, self_destruct,
        )
        await ctx.send(
            f"✅ Added a **{event}** message in {channel.mention}"
            + (f" (auto-deletes after {self_destruct}s)." if self_destruct else "."),
        )

    @sysmsg.command(name="remove")
    async def remove(self, ctx, event: str, channel: discord.TextChannel):
        """Remove all system messages of an event in a channel."""
        event = event.lower()
        if event not in EVENTS:
            return await ctx.send(f"❌ Event must be one of: {', '.join(EVENTS)}")
        result = await self.bot.db.execute(
            "DELETE FROM system_messages WHERE guild_id=$1 AND event=$2 AND channel_id=$3",
            ctx.guild.id, event, channel.id,
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        await ctx.send(f"✅ Removed {n} **{event}** message(s) from {channel.mention}.")

    @sysmsg.command(name="list")
    async def list_(self, ctx):
        """List all configured system messages."""
        rows = await self.bot.db.fetch(
            "SELECT id, event, channel_id, self_destruct, script FROM system_messages "
            "WHERE guild_id=$1 ORDER BY event, id",
            ctx.guild.id,
        )
        if not rows:
            return await ctx.send("ℹ️ None configured.")
        embed = discord.Embed(title="System Messages", color=discord.Color.blurple())
        for r in rows[:25]:
            ch = ctx.guild.get_channel(r["channel_id"])
            preview = (r["script"][:80] + "…") if len(r["script"]) > 80 else r["script"]
            sd = f" · ⏲ {r['self_destruct']}s" if r["self_destruct"] else ""
            ch_label = ch.mention if ch else f"<#{r['channel_id']}>"
            embed.add_field(
                name=f"#{r['id']} · {r['event']} · {ch_label}{sd}",
                value=f"`{preview}`",
                inline=False,
            )
        await ctx.send(embed=embed)

    @sysmsg.command(name="test")
    async def test(self, ctx, event: str):
        """Preview a system message here using you as the target."""
        event = event.lower()
        if event not in EVENTS:
            return await ctx.send(f"❌ Event must be one of: {', '.join(EVENTS)}")

        rows = await self.bot.db.fetch(
            "SELECT script FROM system_messages WHERE guild_id=$1 AND event=$2",
            ctx.guild.id, event,
        )
        if not rows:
            return await ctx.send(f"❌ No **{event}** message configured.")

        await ctx.send(f"**Preview — {event}:**", delete_after=5)
        for row in rows:
            rendered = embed_script.render(
                row["script"],
                user=ctx.author,
                guild=ctx.guild,
                channel=ctx.channel,
            )
            if rendered.is_empty:
                continue
            await ctx.send(
                content=rendered.content,
                embed=rendered.embed,
                view=rendered.view or discord.utils.MISSING,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )


async def setup(bot):
    await bot.add_cog(SystemMessages(bot))
