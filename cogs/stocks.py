"""Stocks and Forex market panels.

Stocks: Yahoo Finance public API (no key required).
Forex:  Frankfurter API (free, no key required).

Commands:
  stocks [ticker]                  — single stock price lookup
  stocks panel <#ch> [tickers]    — self-updating panel (guild default if no tickers)
  stocks removepanel <#ch>        — stop panel
  stocks setdefault <tickers>     — set guild default ticker list
  forex panel <#ch>               — self-updating USD-base forex panel
  forex removepanel <#ch>         — stop forex panel
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, timedelta

import aiohttp
import discord
from discord.ext import commands, tasks

from utils.checks import is_guild_admin, with_perms

log = logging.getLogger("sentinel.stocks")

SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_panels (
    guild_id   BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    tickers    TEXT NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS stock_defaults (
    guild_id BIGINT PRIMARY KEY,
    tickers  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forex_panels (
    guild_id   BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);
"""

DEFAULT_TICKERS = ["SPY", "AAPL", "TSLA", "NVDA", "MSFT", "AMZN", "META", "GOOGL"]
FOREX_PAIRS = ["EUR", "GBP", "JPY", "CAD", "CHF", "AUD", "CNY", "MXN", "SGD"]
PANEL_REFRESH_MINUTES = 5
CACHE_TTL = 60

YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}


def _arrow(pct: float) -> str:
    if pct > 0:
        return "▲"
    if pct < 0:
        return "▼"
    return "—"


def _fmt_price(price: float) -> str:
    if price >= 1000:
        return f"${price:,.2f}"
    if price >= 1:
        return f"${price:.2f}"
    return f"${price:.4f}"


def _fmt_pct(pct: float) -> str:
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"


class StocksCog(commands.Cog):
    """📈 Stocks & Forex market panels"""

    def __init__(self, bot):
        self.bot = bot
        self._stock_cache: dict[str, dict] = {}
        self._stock_cache_ts: float = 0.0
        self._forex_cache: dict[str, dict] = {}
        self._forex_cache_ts: float = 0.0

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)
        self._refresh_panels.start()

    async def cog_unload(self):
        self._refresh_panels.cancel()

    # ---- data fetching ----

    async def _fetch_quote(self, session: aiohttp.ClientSession, symbol: str) -> dict | None:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        try:
            async with session.get(
                url,
                params={"interval": "1d", "range": "1d"},
                headers=YAHOO_HEADERS,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
            result = (data.get("chart", {}).get("result") or [None])[0]
            if not result:
                return None
            meta = result["meta"]
            current = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if current is None:
                return None
            change_pct = ((current - prev) / prev * 100) if prev else 0.0
            return {
                "symbol": symbol.upper(),
                "name": meta.get("longName") or meta.get("shortName") or symbol.upper(),
                "price": current,
                "change_pct": change_pct,
            }
        except Exception as e:
            log.warning("Yahoo Finance fetch failed for %s: %s", symbol, e)
            return None

    async def get_quotes(self, symbols: list[str]) -> dict[str, dict]:
        now = time.monotonic()
        if now - self._stock_cache_ts < CACHE_TTL:
            cached = {s: self._stock_cache[s] for s in symbols if s in self._stock_cache}
            if len(cached) == len(symbols):
                return cached
        async with aiohttp.ClientSession() as session:
            results = await asyncio.gather(*[self._fetch_quote(session, s) for s in symbols])
        for s, r in zip(symbols, results):
            if r:
                self._stock_cache[s] = r
        self._stock_cache_ts = now
        return {s: self._stock_cache[s] for s in symbols if s in self._stock_cache}

    async def get_forex(self) -> dict[str, dict]:
        now = time.monotonic()
        if now - self._forex_cache_ts < CACHE_TTL and self._forex_cache:
            return self._forex_cache
        pairs = ",".join(FOREX_PAIRS)
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://api.frankfurter.app/latest?from=USD&to={pairs}",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    today_data = await resp.json(content_type=None)
                async with session.get(
                    f"https://api.frankfurter.app/{yesterday}?from=USD&to={pairs}",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    yest_data = await resp.json(content_type=None)
            today_rates = today_data.get("rates", {})
            yest_rates = yest_data.get("rates", {})
            result: dict[str, dict] = {}
            for pair in FOREX_PAIRS:
                t = today_rates.get(pair)
                y = yest_rates.get(pair)
                if t is None:
                    continue
                change_pct = ((t - y) / y * 100) if y else 0.0
                result[pair] = {"rate": t, "change_pct": change_pct}
            self._forex_cache = result
            self._forex_cache_ts = now
            return result
        except Exception as e:
            log.warning("Frankfurter fetch failed: %s", e)
            return self._forex_cache

    # ---- embed builders ----

    def _build_stock_embed(self, tickers: list[str], quotes: dict[str, dict]) -> discord.Embed:
        lines = []
        for t in tickers:
            q = quotes.get(t)
            if q is None:
                lines.append(f"`{t:<6}` — unavailable")
                continue
            arrow = _arrow(q["change_pct"])
            lines.append(
                f"`{t:<6}` {_fmt_price(q['price'])}  {_fmt_pct(q['change_pct'])} {arrow}"
            )
        embed = discord.Embed(
            description="\n".join(lines) or "No data.",
            color=discord.Color(0x1DB954),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name="Market Snapshot")
        embed.set_footer(text="Yahoo Finance · refreshes every 5m")
        return embed

    def _build_forex_embed(self, rates: dict[str, dict]) -> discord.Embed:
        lines = []
        for pair, d in rates.items():
            rate = d["rate"]
            rate_str = f"{rate:.4f}" if rate < 100 else f"{rate:.2f}"
            arrow = _arrow(d["change_pct"])
            lines.append(
                f"`USD/{pair}` {rate_str:>10}  {_fmt_pct(d['change_pct'])} {arrow}"
            )
        embed = discord.Embed(
            description="\n".join(lines) or "No data.",
            color=discord.Color(0x5865F2),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name="Forex — USD Base")
        embed.set_footer(text="Frankfurter API · refreshes every 5m")
        return embed

    # ---- refresh loop ----

    @tasks.loop(minutes=PANEL_REFRESH_MINUTES)
    async def _refresh_panels(self):
        stock_rows = await self.bot.db.fetch(
            "SELECT guild_id, channel_id, message_id, tickers FROM stock_panels"
        )
        if stock_rows:
            all_tickers: set[str] = set()
            for r in stock_rows:
                all_tickers.update(r["tickers"].split(","))
            quotes = await self.get_quotes(list(all_tickers))
            for r in stock_rows:
                tickers = r["tickers"].split(",")
                channel = self.bot.get_channel(r["channel_id"])
                if channel is None:
                    continue
                try:
                    msg = await channel.fetch_message(r["message_id"])
                    await msg.edit(embed=self._build_stock_embed(tickers, quotes))
                except discord.NotFound:
                    await self.bot.db.execute(
                        "DELETE FROM stock_panels WHERE guild_id=$1 AND channel_id=$2",
                        r["guild_id"], r["channel_id"],
                    )
                except discord.HTTPException:
                    pass

        forex_rows = await self.bot.db.fetch(
            "SELECT guild_id, channel_id, message_id FROM forex_panels"
        )
        if forex_rows:
            rates = await self.get_forex()
            embed = self._build_forex_embed(rates)
            for r in forex_rows:
                channel = self.bot.get_channel(r["channel_id"])
                if channel is None:
                    continue
                try:
                    msg = await channel.fetch_message(r["message_id"])
                    await msg.edit(embed=embed)
                except discord.NotFound:
                    await self.bot.db.execute(
                        "DELETE FROM forex_panels WHERE guild_id=$1 AND channel_id=$2",
                        r["guild_id"], r["channel_id"],
                    )
                except discord.HTTPException:
                    pass

    @_refresh_panels.before_loop
    async def _before_refresh(self):
        await self.bot.wait_until_ready()

    # ---- helpers ----

    async def _get_default_tickers(self, guild_id: int) -> list[str]:
        row = await self.bot.db.fetchrow(
            "SELECT tickers FROM stock_defaults WHERE guild_id=$1", guild_id
        )
        return row["tickers"].split(",") if row else DEFAULT_TICKERS

    # ---- stocks commands ----

    @commands.group(name="stocks", aliases=["stock", "market"], invoke_without_command=True)
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def stocks(self, ctx, *, ticker: str = None):
        """Stock price lookup and panel commands."""
        if ticker:
            t = ticker.strip().upper()
            quotes = await self.get_quotes([t])
            if t not in quotes:
                return await ctx.send(f"❌ Couldn't fetch data for `{t}`. Check the ticker symbol.")
            q = quotes[t]
            arrow = _arrow(q["change_pct"])
            embed = discord.Embed(
                title=f"{q['symbol']} — {q['name']}",
                description=f"**{_fmt_price(q['price'])}**  {_fmt_pct(q['change_pct'])} {arrow}",
                color=discord.Color.green() if q["change_pct"] >= 0 else discord.Color.red(),
                timestamp=discord.utils.utcnow(),
            )
            embed.set_footer(text="Yahoo Finance")
            return await ctx.send(embed=embed)

        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        defaults = await self._get_default_tickers(ctx.guild.id)
        await ctx.send(
            f"📈 **Stocks**\n"
            f"`{prefix}stocks <ticker>` · single price card\n"
            f"`{prefix}stocks panel <#channel> [ticker1,ticker2,…]` · self-updating panel\n"
            f"`{prefix}stocks removepanel <#channel>`\n"
            f"`{prefix}stocks setdefault <tickers>` · change default panel list\n"
            f"Defaults: `{', '.join(defaults)}`",
        )

    @stocks.command(name="panel")
    @with_perms(manage_messages=True)
    async def stocks_panel(self, ctx, channel: discord.TextChannel, *, tickers: str = None):
        """Post a self-updating stock panel. Omit tickers to use the guild default list."""
        if tickers:
            ticker_list = [t.strip().upper() for t in tickers.replace(" ", ",").split(",") if t.strip()]
        else:
            ticker_list = await self._get_default_tickers(ctx.guild.id)
        if not ticker_list:
            return await ctx.send("❌ No tickers provided.")
        quotes = await self.get_quotes(ticker_list)
        unknown = [t for t in ticker_list if t not in quotes]
        if unknown:
            return await ctx.send(f"❌ Unknown or unavailable ticker(s): {', '.join(unknown)}")
        embed = self._build_stock_embed(ticker_list, quotes)
        try:
            msg = await channel.send(embed=embed)
        except discord.Forbidden:
            return await ctx.send(f"❌ I can't send in {channel.mention}.")
        await self.bot.db.execute(
            """INSERT INTO stock_panels (guild_id, channel_id, message_id, tickers)
               VALUES ($1,$2,$3,$4)
               ON CONFLICT (guild_id, channel_id) DO UPDATE
               SET message_id=EXCLUDED.message_id, tickers=EXCLUDED.tickers""",
            ctx.guild.id, channel.id, msg.id, ",".join(ticker_list),
        )
        await ctx.send(f"✅ Stock panel posted in {channel.mention} (refreshes every {PANEL_REFRESH_MINUTES}m).")

    @stocks.command(name="removepanel")
    @with_perms(manage_messages=True)
    async def stocks_removepanel(self, ctx, channel: discord.TextChannel):
        """Stop updating a stock panel."""
        result = await self.bot.db.execute(
            "DELETE FROM stock_panels WHERE guild_id=$1 AND channel_id=$2",
            ctx.guild.id, channel.id,
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        if n == 0:
            return await ctx.send("ℹ️ No stock panel in that channel.")
        await ctx.send(f"✅ Stopped updating stock panel in {channel.mention}.")

    @stocks.command(name="setdefault")
    @commands.check(is_guild_admin)
    async def stocks_setdefault(self, ctx, *, tickers: str):
        """Set the default ticker list used when posting a panel with no tickers specified."""
        ticker_list = [t.strip().upper() for t in tickers.replace(" ", ",").split(",") if t.strip()]
        if not ticker_list:
            return await ctx.send("❌ Provide at least one ticker.")
        if len(ticker_list) > 20:
            return await ctx.send("❌ Maximum 20 tickers.")
        await self.bot.db.execute(
            """INSERT INTO stock_defaults (guild_id, tickers) VALUES ($1,$2)
               ON CONFLICT (guild_id) DO UPDATE SET tickers=EXCLUDED.tickers""",
            ctx.guild.id, ",".join(ticker_list),
        )
        await ctx.send(f"✅ Default tickers updated: `{', '.join(ticker_list)}`")

    # ---- forex commands ----

    @commands.group(name="forex", aliases=["fx"], invoke_without_command=True)
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def forex(self, ctx):
        """Forex panel commands (USD base rates)."""
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        await ctx.send(
            f"💱 **Forex**\n"
            f"`{prefix}forex panel <#channel>` · self-updating USD-base rate panel\n"
            f"`{prefix}forex removepanel <#channel>`",
        )

    @forex.command(name="panel")
    @with_perms(manage_messages=True)
    async def forex_panel(self, ctx, channel: discord.TextChannel):
        """Post a self-updating forex panel (USD base, major pairs)."""
        rates = await self.get_forex()
        if not rates:
            return await ctx.send("❌ Couldn't fetch forex data right now, try again in a moment.")
        embed = self._build_forex_embed(rates)
        try:
            msg = await channel.send(embed=embed)
        except discord.Forbidden:
            return await ctx.send(f"❌ I can't send in {channel.mention}.")
        await self.bot.db.execute(
            """INSERT INTO forex_panels (guild_id, channel_id, message_id)
               VALUES ($1,$2,$3)
               ON CONFLICT (guild_id, channel_id) DO UPDATE SET message_id=EXCLUDED.message_id""",
            ctx.guild.id, channel.id, msg.id,
        )
        await ctx.send(f"✅ Forex panel posted in {channel.mention} (refreshes every {PANEL_REFRESH_MINUTES}m).")

    @forex.command(name="removepanel")
    @with_perms(manage_messages=True)
    async def forex_removepanel(self, ctx, channel: discord.TextChannel):
        """Stop updating a forex panel."""
        result = await self.bot.db.execute(
            "DELETE FROM forex_panels WHERE guild_id=$1 AND channel_id=$2",
            ctx.guild.id, channel.id,
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        if n == 0:
            return await ctx.send("ℹ️ No forex panel in that channel.")
        await ctx.send(f"✅ Stopped updating forex panel in {channel.mention}.")


async def setup(bot):
    await bot.add_cog(StocksCog(bot))
