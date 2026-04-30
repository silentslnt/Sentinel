"""AFK system.

When a user runs /afk, they're flagged as AFK in the current guild. After that:
  - When they post a message, AFK is auto-cleared and a "welcome back" reply is sent
    (auto-deletes after 8s).
  - When someone else @-mentions an AFK user, the bot replies with the AFK reason
    and how long ago it was set (rate-limited to one notice per AFK user per channel
    per 30s to avoid spam).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

SCHEMA = """
CREATE TABLE IF NOT EXISTS afk_users (
    guild_id BIGINT NOT NULL,
    user_id  BIGINT NOT NULL,
    reason   TEXT,
    set_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id)
);
"""

MENTION_NOTICE_COOLDOWN = 30.0  # seconds


class AFK(commands.Cog):
    """💤 AFK status"""

    def __init__(self, bot):
        self.bot = bot
        # (guild_id, user_id) -> {"reason": str, "set_at": datetime}
        self._cache: dict[tuple[int, int], dict] = {}
        # (guild_id, channel_id, target_user_id) -> last notice monotonic
        self._notice_cooldown: dict[tuple[int, int, int], float] = {}

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)
        rows = await self.bot.db.fetch("SELECT guild_id, user_id, reason, set_at FROM afk_users")
        self._cache = {
            (r["guild_id"], r["user_id"]): {"reason": r["reason"], "set_at": r["set_at"]}
            for r in rows
        }

    @app_commands.command(name="afk", description="Set yourself as AFK")
    @app_commands.guild_only()
    @app_commands.describe(reason="Optional reason shown to people who ping you")
    async def afk(self, interaction: discord.Interaction, reason: Optional[str] = None):
        reason = (reason or "AFK")[:200]
        now = datetime.now(timezone.utc)
        await self.bot.db.execute(
            """INSERT INTO afk_users (guild_id, user_id, reason, set_at)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (guild_id, user_id) DO UPDATE
               SET reason = EXCLUDED.reason, set_at = EXCLUDED.set_at""",
            interaction.guild_id, interaction.user.id, reason, now,
        )
        self._cache[(interaction.guild_id, interaction.user.id)] = {"reason": reason, "set_at": now}
        await interaction.response.send_message(f"💤 You're now AFK: **{reason}**", ephemeral=False)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        # 1. If the author is AFK, clear and welcome back.
        key = (message.guild.id, message.author.id)
        if key in self._cache:
            entry = self._cache.pop(key)
            await self.bot.db.execute(
                "DELETE FROM afk_users WHERE guild_id=$1 AND user_id=$2",
                message.guild.id, message.author.id,
            )
            try:
                away_for = datetime.now(timezone.utc) - entry["set_at"]
                away_secs = int(away_for.total_seconds())
                msg = await message.channel.send(
                    f"👋 Welcome back {message.author.mention} — you were AFK for {_fmt_duration(away_secs)}.",
                    delete_after=8,
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        # 2. If anyone they @-mentioned is AFK, post a notice.
        if not message.mentions:
            return
        for mentioned in message.mentions:
            mkey = (message.guild.id, mentioned.id)
            entry = self._cache.get(mkey)
            if entry is None:
                continue
            cd_key = (message.guild.id, message.channel.id, mentioned.id)
            now_mono = time.monotonic()
            last = self._notice_cooldown.get(cd_key, 0.0)
            if now_mono - last < MENTION_NOTICE_COOLDOWN:
                continue
            self._notice_cooldown[cd_key] = now_mono
            try:
                set_ts = int(entry["set_at"].timestamp())
                await message.channel.send(
                    f"💤 {mentioned.display_name} is AFK: **{entry['reason']}** — "
                    f"set <t:{set_ts}:R>"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass


def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        h, m = divmod(seconds, 3600)
        return f"{h}h {m // 60}m"
    d, rem = divmod(seconds, 86400)
    return f"{d}d {rem // 3600}h"


async def setup(bot):
    await bot.add_cog(AFK(bot))
