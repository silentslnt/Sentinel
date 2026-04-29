"""Auto-responder.

When a user message contains a configured trigger, the bot responds with the
configured script (plain text or full embed). Triggers can be:
  - exact match (full message equals trigger)
  - contains   (default; trigger is a substring of the message)

Optional restrictions: a role and/or channel that the message must come from.
"""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import embed_script

log = logging.getLogger("sentinel.autoresponder")

SCHEMA = """
CREATE TABLE IF NOT EXISTS autoresponders (
    id              BIGSERIAL PRIMARY KEY,
    guild_id        BIGINT NOT NULL,
    trigger         TEXT NOT NULL,
    response        TEXT NOT NULL,
    match_type      TEXT NOT NULL DEFAULT 'contains',  -- 'contains' | 'exact'
    case_sensitive  BOOLEAN NOT NULL DEFAULT FALSE,
    role_id         BIGINT,
    channel_id      BIGINT
);

CREATE INDEX IF NOT EXISTS autoresponders_guild ON autoresponders (guild_id);
"""

MATCH_CHOICES = [
    app_commands.Choice(name="contains (substring)", value="contains"),
    app_commands.Choice(name="exact (full match)", value="exact"),
]


class Autoresponder(commands.Cog):
    """💬 Auto-responder"""

    def __init__(self, bot):
        self.bot = bot
        # guild_id -> list[row dict]
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
            return  # only one rule fires per message

    autoresponder = app_commands.Group(
        name="autoresponder",
        description="Auto-respond to messages",
        default_permissions=discord.Permissions(manage_messages=True),
        guild_only=True,
    )

    @autoresponder.command(name="add", description="Add an auto-responder rule")
    @app_commands.choices(match=MATCH_CHOICES)
    @app_commands.describe(
        trigger="What to look for in messages",
        response="Reply (plain text or embed script)",
        match="contains (default) or exact",
        case_sensitive="Match case-sensitively (default off)",
        role="Only fire when sender has this role (optional)",
        channel="Only fire in this channel (optional)",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        trigger: str,
        response: str,
        match: Optional[app_commands.Choice[str]] = None,
        case_sensitive: bool = False,
        role: Optional[discord.Role] = None,
        channel: Optional[discord.TextChannel] = None,
    ):
        match_type = match.value if match else "contains"
        await self.bot.db.execute(
            """INSERT INTO autoresponders (guild_id, trigger, response, match_type, case_sensitive, role_id, channel_id)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            interaction.guild_id, trigger, response, match_type, case_sensitive,
            role.id if role else None, channel.id if channel else None,
        )
        await self._refresh()
        await interaction.response.send_message(
            f"✅ Added auto-responder for `{trigger}` ({match_type}).", ephemeral=True,
        )

    @autoresponder.command(name="remove", description="Remove an auto-responder by ID")
    async def remove(self, interaction: discord.Interaction, id: int):
        result = await self.bot.db.execute(
            "DELETE FROM autoresponders WHERE id=$1 AND guild_id=$2",
            id, interaction.guild_id,
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        if n == 0:
            return await interaction.response.send_message("❌ No auto-responder with that ID.", ephemeral=True)
        await self._refresh()
        await interaction.response.send_message("✅ Removed.", ephemeral=True)

    @autoresponder.command(name="list", description="List auto-responders")
    async def list_(self, interaction: discord.Interaction):
        rows = self._cache.get(interaction.guild_id, [])
        if not rows:
            return await interaction.response.send_message("ℹ️ None configured.", ephemeral=True)
        lines = []
        for r in rows[:25]:
            constraints = []
            if r["channel_id"]:
                ch = interaction.guild.get_channel(r["channel_id"])
                constraints.append(f"in {ch.mention if ch else r['channel_id']}")
            if r["role_id"]:
                role = interaction.guild.get_role(r["role_id"])
                constraints.append(f"role {role.mention if role else r['role_id']}")
            tail = (" · " + ", ".join(constraints)) if constraints else ""
            lines.append(f"`#{r['id']}` **{r['match_type']}** `{r['trigger'][:40]}` → `{r['response'][:40]}`{tail}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Autoresponder(bot))
