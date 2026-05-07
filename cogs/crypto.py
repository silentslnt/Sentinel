"""Crypto prices + alerts.

Pulls live data from CoinGecko's public API (no key required, rate-limited
~10–50 calls/min on the free tier — we batch a single call every 60s for the
whole bot, then satisfy panel renders + alert checks from the cached snapshot).

Features:
  - `/crypto <coin>` — slash command, single-coin price card
  - `.crypto panel <#channel> <coin1,coin2,…>` — posts a self-updating multi-coin
    embed in the channel; refreshes every 5 minutes. Has Make Alert / View Alerts
    buttons.
  - Make Alert opens a modal: coin, direction, threshold. Alerts fire as DMs.
  - View Alerts shows the user's active alerts and lets them remove individual
    ones.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.checks import with_perms

log = logging.getLogger("sentinel.crypto")

SCHEMA = """
CREATE TABLE IF NOT EXISTS crypto_panels (
    guild_id   BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    coins      TEXT NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS crypto_alerts (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    coin       TEXT   NOT NULL,
    direction  TEXT   NOT NULL CHECK (direction IN ('above','below')),
    threshold  DOUBLE PRECISION NOT NULL,
    last_fired TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS crypto_alerts_user ON crypto_alerts (user_id);
"""

# Common short symbols → CoinGecko IDs.
SYMBOL_MAP = {
    "btc": "bitcoin", "bitcoin": "bitcoin",
    "eth": "ethereum", "ethereum": "ethereum",
    "ltc": "litecoin", "litecoin": "litecoin",
    "sol": "solana", "solana": "solana",
    "doge": "dogecoin", "dogecoin": "dogecoin",
    "xrp": "ripple", "ripple": "ripple",
    "ada": "cardano", "cardano": "cardano",
    "bnb": "binancecoin", "binancecoin": "binancecoin",
    "matic": "matic-network", "polygon": "matic-network",
    "avax": "avalanche-2", "avalanche": "avalanche-2",
    "trx": "tron", "tron": "tron",
    "link": "chainlink", "chainlink": "chainlink",
    "shib": "shiba-inu", "shibainu": "shiba-inu",
    "usdt": "tether", "tether": "tether",
    "usdc": "usd-coin",
    "dot": "polkadot", "polkadot": "polkadot",
    "atom": "cosmos", "cosmos": "cosmos",
    "ton": "the-open-network",
    "xmr": "monero", "monero": "monero",
}

CACHE_TTL = 60  # seconds — single source of truth for all panel/alert checks
PANEL_REFRESH_MINUTES = 5
ALERT_CHECK_MINUTES = 1


def _resolve(symbol_or_id: str) -> str:
    s = symbol_or_id.strip().lower()
    return SYMBOL_MAP.get(s, s)  # if unknown, pass through and let CoinGecko 404


def _fmt_money(n: float) -> str:
    if n >= 1000:
        return f"${n:,.2f}"
    if n >= 1:
        return f"${n:,.4f}".rstrip("0").rstrip(".")
    return f"${n:.6f}".rstrip("0").rstrip(".")


def _fmt_pct(p: Optional[float]) -> str:
    if p is None:
        return "—"
    sign = "+" if p >= 0 else ""
    arrow = "📈" if p >= 0 else "📉"
    return f"{arrow} {sign}{p:.2f}%"


# ---------------- Persistent buttons ----------------

class MakeAlertButton(discord.ui.DynamicItem[discord.ui.Button],
                      template=r"sentinel:cryptoalert:make"):
    def __init__(self):
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label="Make Alert",
                emoji="🔔",
                custom_id="sentinel:cryptoalert:make",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_AlertModal())


class ViewAlertsButton(discord.ui.DynamicItem[discord.ui.Button],
                       template=r"sentinel:cryptoalert:view"):
    def __init__(self):
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label="View Alerts",
                emoji="👁",
                custom_id="sentinel:cryptoalert:view",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        rows = await interaction.client.db.fetch(
            "SELECT id, coin, direction, threshold FROM crypto_alerts WHERE user_id=$1 ORDER BY id",
            interaction.user.id,
        )
        if not rows:
            return await interaction.response.send_message("ℹ️ No active alerts.", ephemeral=True)
        lines = [
            f"`#{r['id']}` **{r['coin']}** {r['direction']} {_fmt_money(r['threshold'])}"
            for r in rows
        ]
        embed = discord.Embed(
            title="🔔 Your Crypto Alerts",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Use the dropdown to remove an alert.")
        view = _RemoveAlertView(rows)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class _AlertModal(discord.ui.Modal, title="Create Crypto Alert"):
    coin_input = discord.ui.TextInput(label="Coin (e.g. BTC, ETH, SOL)", max_length=32)
    direction_input = discord.ui.TextInput(label="Direction (above / below)", max_length=8)
    threshold_input = discord.ui.TextInput(label="Threshold price (USD)", max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        coin = _resolve(self.coin_input.value)
        direction = self.direction_input.value.strip().lower()
        if direction not in ("above", "below"):
            return await interaction.response.send_message("❌ Direction must be `above` or `below`.", ephemeral=True)
        try:
            threshold = float(self.threshold_input.value.replace(",", "").replace("$", ""))
        except ValueError:
            return await interaction.response.send_message("❌ Threshold must be a number.", ephemeral=True)

        # Validate coin by trying to fetch its price.
        cog: "Crypto" = interaction.client.get_cog("Crypto")  # type: ignore
        if cog is None:
            return await interaction.response.send_message("❌ Crypto unavailable.", ephemeral=True)
        snap = await cog.get_snapshot([coin])
        if coin not in snap:
            return await interaction.response.send_message(
                f"❌ Couldn't find coin `{coin}`. Try a different ticker.", ephemeral=True,
            )

        await interaction.client.db.execute(
            "INSERT INTO crypto_alerts (user_id, coin, direction, threshold) VALUES ($1, $2, $3, $4)",
            interaction.user.id, coin, direction, threshold,
        )
        await interaction.response.send_message(
            f"✅ Alert set: notify when **{coin}** is **{direction}** **{_fmt_money(threshold)}**.\n"
            f"Current price: {_fmt_money(snap[coin]['current_price'])}",
            ephemeral=True,
        )


class _RemoveAlertView(discord.ui.View):
    def __init__(self, rows):
        super().__init__(timeout=120)
        options = [
            discord.SelectOption(
                label=f"#{r['id']} · {r['coin']} {r['direction']} ${r['threshold']:g}"[:100],
                value=str(r["id"]),
            )
            for r in rows[:25]
        ]
        select = discord.ui.Select(placeholder="Pick an alert to remove…", options=options)
        select.callback = self._cb
        self.add_item(select)

    async def _cb(self, interaction: discord.Interaction):
        sid = int(interaction.data["values"][0])
        result = await interaction.client.db.execute(
            "DELETE FROM crypto_alerts WHERE id=$1 AND user_id=$2",
            sid, interaction.user.id,
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        if n == 0:
            return await interaction.response.send_message("❌ Already gone.", ephemeral=True)
        await interaction.response.send_message(f"✅ Removed alert #{sid}.", ephemeral=True)


# ---------------- Cog ----------------

class Crypto(commands.Cog):
    """💰 Crypto prices + alerts"""

    def __init__(self, bot):
        self.bot = bot
        self._http: Optional[aiohttp.ClientSession] = None
        self._cache: dict[str, dict] = {}
        self._cache_at: float = 0.0
        self._lock = asyncio.Lock()

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)
        self._http = aiohttp.ClientSession(headers={"User-Agent": "Sentinel-Bot"})
        self.bot.add_dynamic_items(MakeAlertButton, ViewAlertsButton)
        self.refresh_panels.start()
        self.check_alerts.start()

    async def cog_unload(self):
        self.refresh_panels.cancel()
        self.check_alerts.cancel()
        if self._http is not None:
            await self._http.close()

    # ---------- price snapshot ----------

    async def get_snapshot(self, coins: list[str]) -> dict[str, dict]:
        """Return a {coin_id: data} dict. Caches the union of all requested coins for CACHE_TTL."""
        async with self._lock:
            if time.monotonic() - self._cache_at < CACHE_TTL:
                # Cache is warm — but make sure it covers the requested coins.
                missing = [c for c in coins if c not in self._cache]
                if not missing:
                    return {c: self._cache[c] for c in coins if c in self._cache}
                # Add the missing ones to the next fetch.
                coins = list(set(self._cache.keys()) | set(coins))

            params = {
                "vs_currency": "usd",
                "ids": ",".join(coins),
                "price_change_percentage": "24h",
            }
            try:
                async with self._http.get(
                    "https://api.coingecko.com/api/v3/coins/markets",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status != 200:
                        log.warning("CoinGecko %s: %s", r.status, await r.text())
                        return {c: self._cache[c] for c in coins if c in self._cache}
                    data = await r.json()
            except (aiohttp.ClientError, TimeoutError, ValueError):
                log.exception("CoinGecko fetch failed")
                return {c: self._cache[c] for c in coins if c in self._cache}

            new_cache = {}
            for entry in data:
                cid = entry.get("id")
                if cid:
                    new_cache[cid] = entry
            # Merge so we don't drop coins that weren't in this request.
            self._cache.update(new_cache)
            self._cache_at = time.monotonic()
            return {c: self._cache[c] for c in coins if c in self._cache}

    def _build_card(self, data: dict) -> str:
        price = data.get("current_price") or 0
        ch = data.get("price_change_percentage_24h")
        low = data.get("low_24h")
        high = data.get("high_24h")
        return (
            f"{_fmt_pct(ch)}\n"
            f"🔻 {_fmt_money(low) if low else '—'}\n"
            f"🔺 {_fmt_money(high) if high else '—'}"
        )

    def _build_panel_embed(self, coin_ids: list[str], snap: dict[str, dict]) -> discord.Embed:
        embed = discord.Embed(
            title="CRYPTO RATES",
            description=f"Last updated: <t:{int(time.time())}:R>",
            color=discord.Color(0x2B2D31),  # Discord-dark-grey
        )
        for cid in coin_ids:
            d = snap.get(cid)
            if d is None:
                embed.add_field(name=f"❓ {cid}", value="_unavailable_", inline=False)
                continue
            symbol = (d.get("symbol") or "").upper()
            name = d.get("name") or cid
            price = d.get("current_price") or 0
            embed.add_field(
                name=f"{symbol} · {name} — {_fmt_money(price)}",
                value=self._build_card(d),
                inline=False,
            )
        return embed

    @staticmethod
    def _panel_view() -> discord.ui.View:
        v = discord.ui.View(timeout=None)
        v.add_item(MakeAlertButton())
        v.add_item(ViewAlertsButton())
        return v

    # ---------- background loops ----------

    @tasks.loop(minutes=PANEL_REFRESH_MINUTES)
    async def refresh_panels(self):
        rows = await self.bot.db.fetch("SELECT * FROM crypto_panels")
        if not rows:
            return
        all_coins: set[str] = set()
        for r in rows:
            all_coins.update(c.strip() for c in r["coins"].split(",") if c.strip())
        snap = await self.get_snapshot(sorted(all_coins))
        for r in rows:
            channel = self.bot.get_channel(r["channel_id"])
            if channel is None:
                continue
            try:
                msg = await channel.fetch_message(r["message_id"])
            except (discord.NotFound, discord.Forbidden):
                continue
            coin_ids = [c.strip() for c in r["coins"].split(",") if c.strip()]
            embed = self._build_panel_embed(coin_ids, snap)
            try:
                await msg.edit(embed=embed, view=self._panel_view())
            except discord.HTTPException:
                pass

    @refresh_panels.before_loop
    async def _before_panels(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=ALERT_CHECK_MINUTES)
    async def check_alerts(self):
        rows = await self.bot.db.fetch("SELECT * FROM crypto_alerts")
        if not rows:
            return
        coins = sorted({r["coin"] for r in rows})
        snap = await self.get_snapshot(coins)
        for r in rows:
            data = snap.get(r["coin"])
            if data is None:
                continue
            price = data.get("current_price")
            if price is None:
                continue
            triggered = (
                (r["direction"] == "above" and price >= r["threshold"])
                or (r["direction"] == "below" and price <= r["threshold"])
            )
            if not triggered:
                continue
            # Don't re-fire an alert that already fired in the last hour.
            last = r["last_fired"]
            if last is not None and (discord.utils.utcnow() - last).total_seconds() < 3600:
                continue
            user = self.bot.get_user(r["user_id"]) or await self._safe_fetch_user(r["user_id"])
            if user is None:
                continue
            embed = discord.Embed(
                title=f"🔔 Alert · {(data.get('symbol') or '').upper()}",
                description=(
                    f"**{data.get('name', r['coin'])}** is now {_fmt_money(price)}\n"
                    f"Crossed your **{r['direction']}** threshold of {_fmt_money(r['threshold'])}."
                ),
                color=discord.Color.green() if r["direction"] == "above" else discord.Color.red(),
            )
            try:
                await user.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass
            await self.bot.db.execute(
                "UPDATE crypto_alerts SET last_fired=now() WHERE id=$1", r["id"],
            )

    @check_alerts.before_loop
    async def _before_alerts(self):
        await self.bot.wait_until_ready()

    async def _safe_fetch_user(self, uid: int):
        try:
            return await self.bot.fetch_user(uid)
        except (discord.NotFound, discord.HTTPException):
            return None

    # ---------- slash command (single coin) ----------

    @app_commands.command(name="crypto", description="Show the current price of a cryptocurrency")
    @app_commands.describe(coin="Symbol or name (e.g. BTC, ETH, SOL)")
    @app_commands.checks.cooldown(1, 10, key=lambda i: i.user.id)
    async def crypto_slash(self, interaction: discord.Interaction, coin: str):
        await interaction.response.defer()
        cid = _resolve(coin)
        snap = await self.get_snapshot([cid])
        if cid not in snap:
            return await interaction.followup.send(f"❌ Couldn't find `{coin}`.")
        d = snap[cid]
        embed = discord.Embed(
            title=f"{(d.get('symbol') or '').upper()} · {d.get('name') or cid}",
            description=f"**{_fmt_money(d.get('current_price') or 0)}**\n{self._build_card(d)}",
            color=discord.Color(0x2B2D31),
            timestamp=discord.utils.utcnow(),
        )
        if d.get("image"):
            embed.set_thumbnail(url=d["image"])
        await interaction.followup.send(embed=embed)

    # ---------- prefix admin commands ----------

    @commands.group(name="crypto", aliases=["coin", "price"], invoke_without_command=True)
    @commands.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def crypto(self, ctx, *, coin: Optional[str] = None):
        """Crypto utilities. Without a subcommand, show price for <coin>."""
        if coin:
            cid = _resolve(coin)
            snap = await self.get_snapshot([cid])
            if cid not in snap:
                return await ctx.send(f"❌ Couldn't find `{coin}`.")
            d = snap[cid]
            embed = discord.Embed(
                title=f"{(d.get('symbol') or '').upper()} · {d.get('name') or cid}",
                description=f"**{_fmt_money(d.get('current_price') or 0)}**\n{self._build_card(d)}",
                color=discord.Color(0x2B2D31),
                timestamp=discord.utils.utcnow(),
            )
            if d.get("image"):
                embed.set_thumbnail(url=d["image"])
            return await ctx.send(embed=embed)
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        await ctx.send(
            f"💰 **Crypto**\n"
            f"`{prefix}crypto <coin>` · single-coin price\n"
            f"`{prefix}crypto panel <#channel> <coin1,coin2,…>` · self-updating multi-coin embed\n"
            f"`{prefix}crypto removepanel <#channel>` · remove a panel\n"
            f"`{prefix}crypto alerts` · your active alerts (DM)\n"
            f"\nClick **🔔 Make Alert** on any panel to set a price alert.",
        )

    @crypto.command(name="panel")
    @with_perms(manage_messages=True)
    async def panel(self, ctx, channel: discord.TextChannel, *, coins: str):
        """Post a self-updating multi-coin embed in <channel>. Coins comma-separated."""
        coin_ids = [_resolve(c) for c in coins.replace(" ", ",").split(",") if c.strip()]
        if not coin_ids:
            return await ctx.send("❌ Provide at least one coin.")
        snap = await self.get_snapshot(coin_ids)
        unknown = [c for c in coin_ids if c not in snap]
        if unknown:
            return await ctx.send(f"❌ Unknown coin(s): {', '.join(unknown)}")

        embed = self._build_panel_embed(coin_ids, snap)
        try:
            msg = await channel.send(embed=embed, view=self._panel_view())
        except discord.Forbidden:
            return await ctx.send(f"❌ I can't send in {channel.mention}.")
        await self.bot.db.execute(
            """INSERT INTO crypto_panels (guild_id, channel_id, message_id, coins)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (guild_id, channel_id) DO UPDATE
               SET message_id=EXCLUDED.message_id, coins=EXCLUDED.coins""",
            ctx.guild.id, channel.id, msg.id, ",".join(coin_ids),
        )
        await ctx.send(f"✅ Panel posted in {channel.mention} (refreshes every {PANEL_REFRESH_MINUTES}m).")

    @crypto.command(name="removepanel")
    @with_perms(manage_messages=True)
    async def removepanel(self, ctx, channel: discord.TextChannel):
        """Stop updating a crypto panel in <channel>."""
        result = await self.bot.db.execute(
            "DELETE FROM crypto_panels WHERE guild_id=$1 AND channel_id=$2",
            ctx.guild.id, channel.id,
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        if n == 0:
            return await ctx.send("ℹ️ No panel in that channel.")
        await ctx.send(f"✅ Stopped updating panel in {channel.mention}.")

    @crypto.command(name="alerts")
    async def alerts(self, ctx):
        """Show your active alerts (sent as DM)."""
        rows = await self.bot.db.fetch(
            "SELECT id, coin, direction, threshold FROM crypto_alerts WHERE user_id=$1 ORDER BY id",
            ctx.author.id,
        )
        if not rows:
            return await ctx.send("ℹ️ No active alerts. Click 🔔 Make Alert on a panel.")
        lines = [
            f"`#{r['id']}` **{r['coin']}** {r['direction']} {_fmt_money(r['threshold'])}"
            for r in rows
        ]
        try:
            await ctx.author.send("\n".join(lines))
            await ctx.send("📬 Sent to your DMs.")
        except discord.Forbidden:
            await ctx.send("\n".join(lines))


async def setup(bot):
    await bot.add_cog(Crypto(bot))
