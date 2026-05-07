"""Vanity / server-tag role detector. Prefix-only configuration.

Two detection modes (either or both can be enabled per guild):
  1. status   — Bleed-style: scan custom status text for a configured substring.
  2. tag      — Discord's "guild tag" feature: user.primary_guild points at this guild.

When a member matches, configured reward role(s) are granted. When they no longer
match, the roles are revoked. Detection runs:
  - on_presence_update     (custom status text changes)
  - on_member_update       (member profile changes)
  - on_user_update          (primary_guild changes carry through here)
  - periodic full sweep    (every 10 min)
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands, tasks

from utils.checks import is_guild_admin

log = logging.getLogger("sentinel.vanity")

SCHEMA = """
CREATE TABLE IF NOT EXISTS vanity_config (
    guild_id          BIGINT PRIMARY KEY,
    substring         TEXT,
    mode              TEXT NOT NULL DEFAULT 'both',
    award_channel_id  BIGINT,
    message           TEXT
);

CREATE TABLE IF NOT EXISTS vanity_roles (
    guild_id BIGINT NOT NULL,
    role_id  BIGINT NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS vanity_granted (
    guild_id BIGINT NOT NULL,
    user_id  BIGINT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
"""

VALID_MODES = ("status", "tag", "both")


def _custom_status_text(member: discord.Member) -> str:
    for activity in member.activities:
        if isinstance(activity, discord.CustomActivity):
            return activity.state or ""
    return ""


def _has_guild_tag(member: discord.Member, guild_id: int) -> bool:
    primary = getattr(member, "primary_guild", None)
    if primary is None:
        return False
    if not getattr(primary, "identity_enabled", True):
        return False
    return getattr(primary, "id", None) == guild_id


class Vanity(commands.Cog):
    """🏷️ Server-tag / vanity role automation"""

    def __init__(self, bot):
        self.bot = bot
        self._cfg: dict[int, dict] = {}
        self._roles: dict[int, set[int]] = {}

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)
        await self._refresh_cache()
        self.periodic_sweep.start()

    async def cog_unload(self):
        self.periodic_sweep.cancel()

    async def _refresh_cache(self):
        cfg_rows = await self.bot.db.fetch("SELECT * FROM vanity_config")
        self._cfg = {r["guild_id"]: dict(r) for r in cfg_rows}
        role_rows = await self.bot.db.fetch("SELECT guild_id, role_id FROM vanity_roles")
        self._roles = {}
        for r in role_rows:
            self._roles.setdefault(r["guild_id"], set()).add(r["role_id"])

    def _enabled_for(self, guild_id: int) -> bool:
        cfg = self._cfg.get(guild_id)
        if not cfg:
            return False
        roles = self._roles.get(guild_id)
        if not roles:
            return False
        if cfg["mode"] == "status" and not cfg.get("substring"):
            return False
        return True

    def _matches(self, member: discord.Member) -> bool:
        cfg = self._cfg.get(member.guild.id)
        if not cfg:
            return False
        mode = cfg["mode"]
        substring = (cfg.get("substring") or "").lower()

        if mode in ("tag", "both") and _has_guild_tag(member, member.guild.id):
            return True
        if mode in ("status", "both") and substring:
            if substring in _custom_status_text(member).lower():
                return True
        return False

    async def _evaluate(self, member: discord.Member):
        if member.bot or member.guild is None:
            return
        guild_id = member.guild.id
        if not self._enabled_for(guild_id):
            return

        configured_role_ids = self._roles.get(guild_id, set())
        guild_roles = [member.guild.get_role(rid) for rid in configured_role_ids]
        guild_roles = [r for r in guild_roles if r is not None and r < member.guild.me.top_role]
        if not guild_roles:
            return

        member_role_ids = {r.id for r in member.roles}
        matches = self._matches(member)

        if matches:
            to_add = [r for r in guild_roles if r.id not in member_role_ids]
            if to_add:
                try:
                    await member.add_roles(*to_add, reason="Vanity match")
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning("vanity add_roles failed for %s: %s", member, e)
                    return
                already = await self.bot.db.fetchval(
                    "SELECT 1 FROM vanity_granted WHERE guild_id=$1 AND user_id=$2",
                    guild_id, member.id,
                )
                if not already:
                    await self.bot.db.execute(
                        "INSERT INTO vanity_granted (guild_id, user_id) VALUES ($1, $2) "
                        "ON CONFLICT DO NOTHING",
                        guild_id, member.id,
                    )
                    await self._send_award_message(member)
        else:
            to_remove = [r for r in guild_roles if r.id in member_role_ids]
            if to_remove:
                try:
                    await member.remove_roles(*to_remove, reason="Vanity no longer matches")
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning("vanity remove_roles failed for %s: %s", member, e)
                await self.bot.db.execute(
                    "DELETE FROM vanity_granted WHERE guild_id=$1 AND user_id=$2",
                    guild_id, member.id,
                )

    async def _send_award_message(self, member: discord.Member):
        cfg = self._cfg.get(member.guild.id)
        if not cfg:
            return
        channel_id = cfg.get("award_channel_id")
        message = cfg.get("message")
        if not channel_id or not message:
            return
        channel = member.guild.get_channel(channel_id)
        if channel is None:
            return
        text = message.replace("{user}", member.mention).replace("{user.name}", member.display_name)
        try:
            await channel.send(text)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ----- listeners -----

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        if _custom_status_text(before) == _custom_status_text(after):
            return
        await self._evaluate(after)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.roles == after.roles and getattr(before, "primary_guild", None) == getattr(after, "primary_guild", None):
            return
        await self._evaluate(after)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        if getattr(before, "primary_guild", None) == getattr(after, "primary_guild", None):
            return
        for guild in self.bot.guilds:
            member = guild.get_member(after.id)
            if member is not None:
                await self._evaluate(member)

    @tasks.loop(minutes=10)
    async def periodic_sweep(self):
        for guild in self.bot.guilds:
            if not self._enabled_for(guild.id):
                continue
            for member in guild.members:
                if member.bot:
                    continue
                try:
                    await self._evaluate(member)
                except Exception:
                    log.exception("periodic_sweep failed for %s", member)
                await asyncio.sleep(0)

    @periodic_sweep.before_loop
    async def _before_sweep(self):
        await self.bot.wait_until_ready()

    # ----- commands -----

    async def _upsert_cfg(self, guild_id: int, **fields):
        await self.bot.db.execute(
            "INSERT INTO vanity_config (guild_id) VALUES ($1) ON CONFLICT DO NOTHING",
            guild_id,
        )
        if fields:
            sets = ", ".join(f"{k}=${i+2}" for i, k in enumerate(fields))
            args = [guild_id, *fields.values()]
            await self.bot.db.execute(
                f"UPDATE vanity_config SET {sets} WHERE guild_id=$1", *args
            )
        await self._refresh_cache()

    @commands.group(name="vanity", aliases=["v"], invoke_without_command=True)
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def vanity(self, ctx):
        """Vanity / server-tag role automation."""
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        await ctx.send(
            f"🏷️ **Vanity / server-tag**\n"
            f"`{prefix}vanity set <substring>` · monitor custom status for substring\n"
            f"`{prefix}vanity mode <status|tag|both>`\n"
            f"`{prefix}vanity channel <#channel>` · award message destination\n"
            f"`{prefix}vanity message <text>` · award text (`{{user}}`, `{{user.name}}`)\n"
            f"`{prefix}vanity role add <@role>` · add reward role\n"
            f"`{prefix}vanity role remove <@role>`\n"
            f"`{prefix}vanity role list`\n"
            f"`{prefix}vanity config`\n"
            f"`{prefix}vanity resync`",
        )

    @vanity.command(name="set")
    async def vanity_set(self, ctx, *, substring: str = ""):
        """Set the substring to monitor in user statuses (e.g. /sentinel)."""
        await self._upsert_cfg(ctx.guild.id, substring=substring or None)
        await ctx.send(
            f"✅ Now monitoring custom statuses for `{substring}`." if substring
            else "✅ Substring cleared.",
        )

    @vanity.command(name="mode")
    async def vanity_mode(self, ctx, mode: str):
        """Set detection mode: status, tag, or both."""
        mode = mode.lower()
        if mode not in VALID_MODES:
            return await ctx.send(f"❌ Mode must be one of: {', '.join(VALID_MODES)}")
        await self._upsert_cfg(ctx.guild.id, mode=mode)
        await ctx.send(f"✅ Detection mode set to **{mode}**.")

    @vanity.command(name="channel")
    async def vanity_channel(self, ctx, channel: discord.TextChannel):
        """Channel where award messages are posted."""
        await self._upsert_cfg(ctx.guild.id, award_channel_id=channel.id)
        await ctx.send(f"✅ Award messages will be posted in {channel.mention}.")

    @vanity.command(name="message")
    async def vanity_message(self, ctx, *, message: str):
        """Award message text (supports {user} and {user.name})."""
        await self._upsert_cfg(ctx.guild.id, message=message)
        await ctx.send("✅ Award message updated.")

    @vanity.command(name="config")
    async def vanity_config(self, ctx):
        """View current vanity configuration."""
        cfg = self._cfg.get(ctx.guild.id)
        roles = self._roles.get(ctx.guild.id, set())
        if not cfg:
            return await ctx.send("ℹ️ Vanity is not configured here yet.")
        embed = discord.Embed(title="🏷️ Vanity Configuration", color=discord.Color.blurple())
        embed.add_field(name="Mode", value=f"`{cfg.get('mode', 'both')}`", inline=True)
        embed.add_field(name="Substring", value=f"`{cfg.get('substring') or '—'}`", inline=True)
        ch = ctx.guild.get_channel(cfg.get("award_channel_id") or 0)
        embed.add_field(name="Award Channel", value=ch.mention if ch else "—", inline=True)
        role_mentions = []
        for rid in roles:
            r = ctx.guild.get_role(rid)
            if r:
                role_mentions.append(r.mention)
        embed.add_field(name=f"Reward Roles ({len(role_mentions)})", value=", ".join(role_mentions) or "—", inline=False)
        embed.add_field(name="Message", value=(cfg.get("message") or "—")[:1024], inline=False)
        await ctx.send(embed=embed)

    @vanity.group(name="role", invoke_without_command=True)
    async def vanity_role(self, ctx):
        """Manage reward roles."""
        await self.vanity(ctx)

    @vanity_role.command(name="add")
    async def role_add(self, ctx, role: discord.Role):
        """Add a reward role."""
        if role >= ctx.guild.me.top_role:
            return await ctx.send("❌ That role is above my highest role — I can't manage it.")
        await self._upsert_cfg(ctx.guild.id)
        await self.bot.db.execute(
            "INSERT INTO vanity_roles (guild_id, role_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            ctx.guild.id, role.id,
        )
        await self._refresh_cache()
        await ctx.send(f"✅ Added {role.mention} as a reward role.")

    @vanity_role.command(name="remove")
    async def role_remove(self, ctx, role: discord.Role):
        """Remove a reward role."""
        await self.bot.db.execute(
            "DELETE FROM vanity_roles WHERE guild_id=$1 AND role_id=$2",
            ctx.guild.id, role.id,
        )
        await self._refresh_cache()
        await ctx.send(f"✅ Removed {role.mention} from reward roles.")

    @vanity_role.command(name="list")
    async def role_list(self, ctx):
        """List all reward roles."""
        roles = self._roles.get(ctx.guild.id, set())
        if not roles:
            return await ctx.send("ℹ️ No reward roles configured.")
        mentions = []
        for rid in roles:
            r = ctx.guild.get_role(rid)
            mentions.append(r.mention if r else f"`<deleted role {rid}>`")
        await ctx.send("Reward roles: " + ", ".join(mentions))

    @vanity.command(name="resync")
    async def vanity_resync(self, ctx):
        """Force re-evaluation of every member in this server."""
        if not self._enabled_for(ctx.guild.id):
            return await ctx.send(
                "❌ Vanity is not fully configured (need at least one reward role)."
            )

        configured_role_ids = self._roles.get(ctx.guild.id, set())
        guild_roles = [ctx.guild.get_role(rid) for rid in configured_role_ids]
        guild_roles = [r for r in guild_roles if r is not None and r < ctx.guild.me.top_role]
        if not guild_roles:
            return await ctx.send("❌ No valid reward roles (check I have a higher role than them).")

        msg = await ctx.send("⏳ Resync started…")

        added: dict[int, int] = {r.id: 0 for r in guild_roles}   # role_id -> count added
        removed: dict[int, int] = {r.id: 0 for r in guild_roles}
        matched = 0

        for member in ctx.guild.members:
            if member.bot:
                continue
            member_role_ids = {r.id for r in member.roles}
            matches = self._matches(member)
            if matches:
                matched += 1
                to_add = [r for r in guild_roles if r.id not in member_role_ids]
                if to_add:
                    try:
                        await member.add_roles(*to_add, reason="Vanity resync")
                        for r in to_add:
                            added[r.id] += 1
                    except (discord.Forbidden, discord.HTTPException):
                        pass
            else:
                to_remove = [r for r in guild_roles if r.id in member_role_ids]
                if to_remove:
                    try:
                        await member.remove_roles(*to_remove, reason="Vanity resync")
                        for r in to_remove:
                            removed[r.id] += 1
                    except (discord.Forbidden, discord.HTTPException):
                        pass
            await asyncio.sleep(0)

        lines = [f"**{matched}** member(s) currently match vanity."]
        for r in guild_roles:
            a = added[r.id]
            rm = removed[r.id]
            parts = []
            if a:
                parts.append(f"+{a} added")
            if rm:
                parts.append(f"-{rm} removed")
            change = f" ({', '.join(parts)})" if parts else " (no change)"
            lines.append(f"{r.mention}{change}")

        await msg.edit(content="\n".join(lines))


async def setup(bot):
    await bot.add_cog(Vanity(bot))
