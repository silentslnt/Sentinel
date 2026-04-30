"""Auto-responder. Prefix-only configuration.

When a user message contains a configured trigger, the bot responds with the
configured script (plain text or full embed). Triggers can be:
  - exact match (full message equals trigger)
  - contains   (default; trigger is a substring of the message)
"""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from utils import embed_script

log = logging.getLogger("sentinel.autoresponder")

SCHEMA = """
CREATE TABLE IF NOT EXISTS autoresponders (
    id              BIGSERIAL PRIMARY KEY,
    guild_id        BIGINT NOT NULL,
    trigger         TEXT NOT NULL,
    response        TEXT NOT NULL,
    match_type      TEXT NOT NULL DEFAULT 'contains',
    case_sensitive  BOOLEAN NOT NULL DEFAULT FALSE,
    role_id         BIGINT,
    channel_id      BIGINT
);

CREATE INDEX IF NOT EXISTS autoresponders_guild ON autoresponders (guild_id);
"""


class Autoresponder(commands.Cog):
    """💬 Auto-responder"""

    def __init__(self, bot):
        self.bot = bot
        self._cache: dict[int, list[dict]] = {}

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)
        await self._refresh()

    async def _refresh(self):
        rows = await self.bot.db.fetch("SELECT * FROM autoresponders")
        self._cache = {}
        for r in rows:
            self._cache.setdefault(r["guild_id"], []).append(dict(r))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        rules = self._cache.get(message.guild.id)
        if not rules:
            return

        for r in rules:
            trigger = r["trigger"] if r["case_sensitive"] else r["trigger"].lower()
            content = message.content if r["case_sensitive"] else message.content.lower()

            matched = (content == trigger) if r["match_type"] == "exact" else (trigger in content)
            if not matched:
                continue

            if r["channel_id"] and message.channel.id != r["channel_id"]:
                continue
            if r["role_id"]:
                if not any(role.id == r["role_id"] for role in message.author.roles):
                    continue

            rendered = embed_script.render(
                r["response"],
                user=message.author,
                guild=message.guild,
                channel=message.channel,
            )
            try:
                await message.channel.send(
                    content=rendered.content,
                    embed=rendered.embed,
                    view=rendered.view or discord.utils.MISSING,
                    reference=message,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
            return

    # ---------------- commands ----------------

    @commands.group(name="autoresponder", aliases=["ar"], invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def autoresponder(self, ctx):
        """Auto-respond to messages."""
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        await ctx.send(
            f"💬 **Auto-responder**\n"
            f"`{prefix}autoresponder add <trigger> | <response>` (use `|` to separate)\n"
            f"`{prefix}autoresponder addexact <trigger> | <response>`\n"
            f"`{prefix}autoresponder remove <id>`\n"
            f"`{prefix}autoresponder list`",
        )

    @autoresponder.command(name="add")
    async def add(self, ctx, *, body: str):
        """Add a contains-match auto-responder. Format: trigger | response"""
        await self._add(ctx, body, "contains")

    @autoresponder.command(name="addexact")
    async def addexact(self, ctx, *, body: str):
        """Add an exact-match auto-responder. Format: trigger | response"""
        await self._add(ctx, body, "exact")

    async def _add(self, ctx, body: str, match_type: str):
        if "|" not in body:
            return await ctx.send("❌ Use `trigger | response` (separator is `|`).")
        trigger, response = (s.strip() for s in body.split("|", 1))
        if not trigger or not response:
            return await ctx.send("❌ Both trigger and response are required.")
        await self.bot.db.execute(
            """INSERT INTO autoresponders (guild_id, trigger, response, match_type)
               VALUES ($1, $2, $3, $4)""",
            ctx.guild.id, trigger, response, match_type,
        )
        await self._refresh()
        await ctx.send(f"✅ Added auto-responder for `{trigger}` ({match_type}).")

    @autoresponder.command(name="remove")
    async def remove(self, ctx, id: int):
        """Remove an auto-responder by ID."""
        result = await self.bot.db.execute(
            "DELETE FROM autoresponders WHERE id=$1 AND guild_id=$2", id, ctx.guild.id,
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        if n == 0:
            return await ctx.send("❌ No auto-responder with that ID.")
        await self._refresh()
        await ctx.send("✅ Removed.")

    @autoresponder.command(name="list")
    async def list_(self, ctx):
        """List auto-responders."""
        rows = self._cache.get(ctx.guild.id, [])
        if not rows:
            return await ctx.send("ℹ️ None configured.")
        lines = []
        for r in rows[:25]:
            constraints = []
            if r["channel_id"]:
                ch = ctx.guild.get_channel(r["channel_id"])
                constraints.append(f"in {ch.mention if ch else r['channel_id']}")
            if r["role_id"]:
                role = ctx.guild.get_role(r["role_id"])
                constraints.append(f"role {role.mention if role else r['role_id']}")
            tail = (" · " + ", ".join(constraints)) if constraints else ""
            lines.append(f"`#{r['id']}` **{r['match_type']}** `{r['trigger'][:40]}` → `{r['response'][:40]}`{tail}")
        await ctx.send("\n".join(lines))


async def setup(bot):
    await bot.add_cog(Autoresponder(bot))
