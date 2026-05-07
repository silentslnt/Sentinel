"""Roblox trending games panel.

Pulls the top 10 games by active player count from the public Roblox API.
Tracks player count changes between refreshes and shows % up/down per game.

Commands:
  roblox panel <#channel>       — post self-updating top-10 panel
  roblox removepanel <#channel> — stop updates
"""
from __future__ import annotations

import logging
import time

import aiohttp
import discord
from discord.ext import commands, tasks

from utils.checks import is_guild_admin, with_perms

log = logging.getLogger("sentinel.roblox")

SCHEMA = """
CREATE TABLE IF NOT EXISTS roblox_panels (
    guild_id   BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);
"""

PANEL_REFRESH_MINUTES = 5
CACHE_TTL = 60
SORTS_URL = "https://apis.roblox.com/explore-api/v1/get-sorts"
GAMES_URL = "https://apis.roblox.com/explore-api/v1/get-games"


def _fmt_players(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _arrow(pct: float) -> str:
    if pct > 0.5:
        return "▲"
    if pct < -0.5:
        return "▼"
    return "—"


class RobloxCog(commands.Cog):
    """🎮 Roblox trending games panel"""

    def __init__(self, bot):
        self.bot = bot
        self._cache: list[dict] = []
        self._cache_ts: float = 0.0
        self._prev_counts: dict[int, int] = {}

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)
        self._refresh_panels.start()

    async def cog_unload(self):
        self._refresh_panels.cancel()

    async def _fetch_trending(self) -> list[dict]:
        now = time.monotonic()
        if now - self._cache_ts < CACHE_TTL and self._cache:
            return self._cache
        headers = {"User-Agent": "Mozilla/5.0"}
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                # Step 1: get sort tokens
                async with session.get(SORTS_URL, timeout=timeout) as resp:
                    if resp.status != 200:
                        log.warning("Roblox sorts API returned %s", resp.status)
                        return self._cache
                    sorts_data = await resp.json(content_type=None)

                # Find the "Popular" sort token (fall back to first sort available)
                sorts = sorts_data.get("sorts", [])
                token = None
                for s in sorts:
                    name = (s.get("name") or s.get("displayName") or "").lower()
                    if "popular" in name:
                        token = s.get("token")
                        break
                if not token and sorts:
                    token = sorts[0].get("token")
                if not token:
                    log.warning("Roblox: no sort token found. Keys: %s", list(sorts_data.keys()))
                    return self._cache

                # Step 2: fetch games for that sort
                async with session.get(
                    GAMES_URL,
                    params={"sortToken": token, "startRows": 0, "maxRows": 10},
                    timeout=timeout,
                ) as resp:
                    if resp.status != 200:
                        log.warning("Roblox games API returned %s", resp.status)
                        return self._cache
                    games_data = await resp.json(content_type=None)

            games = games_data.get("games", [])
            if not games:
                log.warning("Roblox games API returned 0 games. Keys: %s", list(games_data.keys()))
            self._cache = games
            self._cache_ts = now
            return games
        except Exception as e:
            log.warning("Roblox API fetch failed: %s", e)
            return self._cache

    def _build_embed(self, games: list[dict]) -> discord.Embed:
        lines = []
        for i, g in enumerate(games, 1):
            uid = g.get("universeId") or g.get("id") or i
            name = (g.get("name") or "Unknown")[:32]
            players = g.get("playerCount") or 0
            prev = self._prev_counts.get(uid)
            if prev is not None and prev > 0:
                pct = (players - prev) / prev * 100
                arrow = _arrow(pct)
                if abs(pct) >= 0.5:
                    pct_str = f"{'+' if pct > 0 else ''}{pct:.1f}%"
                    trend = f"{arrow} {pct_str}"
                else:
                    trend = "—"
            else:
                trend = "—"
            self._prev_counts[uid] = players
            lines.append(
                f"`#{i:>2}` **{name}**\n"
                f"      {_fmt_players(players)} playing  {trend}"
            )
        embed = discord.Embed(
            description="\n".join(lines) or "No data.",
            color=discord.Color(0xE8432D),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name="Roblox — Top Games Now")
        embed.set_footer(text="Roblox API · refreshes every 5m")
        return embed

    @tasks.loop(minutes=PANEL_REFRESH_MINUTES)
    async def _refresh_panels(self):
        rows = await self.bot.db.fetch(
            "SELECT guild_id, channel_id, message_id FROM roblox_panels"
        )
        if not rows:
            return
        games = await self._fetch_trending()
        if not games:
            return
        embed = self._build_embed(games)
        for r in rows:
            channel = self.bot.get_channel(r["channel_id"])
            if channel is None:
                continue
            try:
                msg = await channel.fetch_message(r["message_id"])
                await msg.edit(embed=embed)
            except discord.NotFound:
                await self.bot.db.execute(
                    "DELETE FROM roblox_panels WHERE guild_id=$1 AND channel_id=$2",
                    r["guild_id"], r["channel_id"],
                )
            except discord.HTTPException:
                pass

    @_refresh_panels.before_loop
    async def _before_refresh(self):
        await self.bot.wait_until_ready()

    @commands.group(name="roblox", aliases=["rbx"], invoke_without_command=True)
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def roblox(self, ctx):
        """Roblox trending games panel."""
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        await ctx.send(
            f"🎮 **Roblox**\n"
            f"`{prefix}roblox panel <#channel>` · self-updating top 10 panel\n"
            f"`{prefix}roblox removepanel <#channel>`",
        )

    @roblox.command(name="panel")
    @with_perms(manage_messages=True)
    async def roblox_panel(self, ctx, channel: discord.TextChannel):
        """Post a self-updating Roblox top 10 trending panel."""
        games = await self._fetch_trending()
        if not games:
            return await ctx.send("❌ Couldn't fetch Roblox data right now, try again.")
        embed = self._build_embed(games)
        try:
            msg = await channel.send(embed=embed)
        except discord.Forbidden:
            return await ctx.send(f"❌ I can't send in {channel.mention}.")
        await self.bot.db.execute(
            """INSERT INTO roblox_panels (guild_id, channel_id, message_id)
               VALUES ($1,$2,$3)
               ON CONFLICT (guild_id, channel_id) DO UPDATE SET message_id=EXCLUDED.message_id""",
            ctx.guild.id, channel.id, msg.id,
        )
        await ctx.send(f"✅ Roblox panel posted in {channel.mention} (refreshes every {PANEL_REFRESH_MINUTES}m).")

    @roblox.command(name="removepanel")
    @with_perms(manage_messages=True)
    async def roblox_removepanel(self, ctx, channel: discord.TextChannel):
        """Stop updating a Roblox panel."""
        result = await self.bot.db.execute(
            "DELETE FROM roblox_panels WHERE guild_id=$1 AND channel_id=$2",
            ctx.guild.id, channel.id,
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        if n == 0:
            return await ctx.send("ℹ️ No Roblox panel in that channel.")
        await ctx.send(f"✅ Stopped updating Roblox panel in {channel.mention}.")


async def setup(bot):
    await bot.add_cog(RobloxCog(bot))
