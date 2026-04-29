"""Vanity / server-tag role detector.

Two detection modes (either or both can be enabled per guild):
  1. status   — Bleed-style: scan custom status text for a configured substring.
  2. tag      — Discord's "guild tag" feature: user.primary_guild points at this guild.

When a member matches, configured reward role(s) are granted. When they no longer
match, the roles are revoked. Detection runs:
  - on_presence_update     (custom status text changes)
  - on_member_update       (member profile changes)
  - on_user_update          (primary_guild changes carry through here)
  - periodic full sweep    (every 10 min, catches missed events / restarts)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("sentinel.vanity")

SCHEMA = """
CREATE TABLE IF NOT EXISTS vanity_config (
    guild_id          BIGINT PRIMARY KEY,
    substring         TEXT,
    mode              TEXT NOT NULL DEFAULT 'both',  -- 'status' | 'tag' | 'both'
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


def _custom_status_text(member: discord.Member) -> str:
    """Return the member's custom status text, or empty string."""
    for activity in member.activities:
        if isinstance(activity, discord.CustomActivity) and activity.name:
            return activity.name
    return ""


def _has_guild_tag(member: discord.Member, guild_id: int) -> bool:
    """Return True if the member's Discord primary-guild tag points at this guild."""
    primary = getattr(member, "primary_guild", None)
    if primary is None:
        return False
    if not getattr(primary, "identity_enabled", False):
        return False
    return getattr(primary, "identity_guild_id", None) == guild_id


class Vanity(commands.Cog):
    """🏷️ Server-tag / vanity role automation"""

    def __init__(self, bot):
        self.bot = bot
        # In-memory cache: guild_id -> config dict
        self._cfg: dict[int, dict] = {}
        # In-memory cache: guild_id -> set of role_id
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
        # At minimum, tag mode works without a substring; status mode needs one.
        if cfg["mode"] in ("status", "both") and not cfg.get("substring"):
            return cfg["mode"] == "both" and True  # tag-only fallback still works in 'both'
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
        """Grant/revoke vanity roles for a single member based on current state."""
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
                # Track grant + post thank-you (only on first grant of any role)
                already_granted = await self.bot.db.fetchval(
                    "SELECT 1 FROM vanity_granted WHERE guild_id=$1 AND user_id=$2",
                    guild_id,
                    member.id,
                )
                if not already_granted:
                    await self.bot.db.execute(
                        "INSERT INTO vanity_granted (guild_id, user_id) VALUES ($1, $2) "
                        "ON CONFLICT DO NOTHING",
                        guild_id,
                        member.id,
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
                    guild_id,
                    member.id,
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
        # Phase 2: plain text with simple {user} substitution. Phase 3 will swap
        # this for the full embed-script parser.
        text = message.replace("{user}", member.mention).replace("{user.name}", member.display_name)
        try:
            await channel.send(text)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ------------------- listeners -------------------

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        if _custom_status_text(before) == _custom_status_text(after):
            return
        await self._evaluate(after)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # Re-evaluate on role/profile changes (covers manual role removal, tag changes
        # that arrive via member update on some clients).
        if before.roles == after.roles and getattr(before, "primary_guild", None) == getattr(after, "primary_guild", None):
            return
        await self._evaluate(after)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        if getattr(before, "primary_guild", None) == getattr(after, "primary_guild", None):
            return
        # primary_guild changed — sweep this user across every guild we share with them.
        for guild in self.bot.guilds:
            member = guild.get_member(after.id)
            if member is not None:
                await self._evaluate(member)

    @tasks.loop(minutes=10)
    async def periodic_sweep(self):
        """Catch any drift from missed gateway events."""
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
                await asyncio.sleep(0)  # yield to the event loop

    @periodic_sweep.before_loop
    async def _before_sweep(self):
        await self.bot.wait_until_ready()

    # ------------------- commands -------------------

    vanity = app_commands.Group(
        name="vanity",
        description="Vanity / server-tag role automation",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    role_group = app_commands.Group(name="role", description="Manage reward roles", parent=vanity)

    async def _upsert_cfg(self, guild_id: int, **fields):
        # Ensure row exists with defaults.
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

    @vanity.command(name="set", description="Set the substring to monitor in user statuses (e.g. /sentinel)")
    @app_commands.describe(substring="The substring to look for in custom status. Empty to clear.")
    async def vanity_set(self, interaction: discord.Interaction, substring: str):
        await self._upsert_cfg(interaction.guild_id, substring=substring or None)
        await interaction.response.send_message(
            f"✅ Now monitoring custom statuses for `{substring}`." if substring
            else "✅ Substring cleared.",
            ephemeral=True,
        )

    @vanity.command(name="mode", description="Set detection mode: status, tag, or both")
    @app_commands.choices(mode=[
        app_commands.Choice(name="status (substring in custom status)", value="status"),
        app_commands.Choice(name="tag (Discord guild tag pointing at this server)", value="tag"),
        app_commands.Choice(name="both", value="both"),
    ])
    async def vanity_mode(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        await self._upsert_cfg(interaction.guild_id, mode=mode.value)
        await interaction.response.send_message(f"✅ Detection mode set to **{mode.value}**.", ephemeral=True)

    @vanity.command(name="channel", description="Channel where award messages are posted")
    async def vanity_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self._upsert_cfg(interaction.guild_id, award_channel_id=channel.id)
        await interaction.response.send_message(f"✅ Award messages will be posted in {channel.mention}.", ephemeral=True)

    @vanity.command(name="message", description="Award message text (supports {user} and {user.name})")
    async def vanity_message(self, interaction: discord.Interaction, message: str):
        await self._upsert_cfg(interaction.guild_id, message=message)
        await interaction.response.send_message("✅ Award message updated.", ephemeral=True)

    @vanity.command(name="config", description="View current vanity configuration")
    async def vanity_config(self, interaction: discord.Interaction):
        cfg = self._cfg.get(interaction.guild_id)
        roles = self._roles.get(interaction.guild_id, set())
        if not cfg:
            return await interaction.response.send_message("ℹ️ Vanity is not configured here yet.", ephemeral=True)
        embed = discord.Embed(title="🏷️ Vanity Configuration", color=discord.Color.blurple())
        embed.add_field(name="Mode", value=f"`{cfg.get('mode', 'both')}`", inline=True)
        embed.add_field(name="Substring", value=f"`{cfg.get('substring') or '—'}`", inline=True)
        ch = interaction.guild.get_channel(cfg.get("award_channel_id") or 0)
        embed.add_field(name="Award Channel", value=ch.mention if ch else "—", inline=True)
        role_mentions = []
        for rid in roles:
            r = interaction.guild.get_role(rid)
            if r:
                role_mentions.append(r.mention)
        embed.add_field(name=f"Reward Roles ({len(role_mentions)})", value=", ".join(role_mentions) or "—", inline=False)
        embed.add_field(name="Message", value=(cfg.get("message") or "—")[:1024], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @role_group.command(name="add", description="Add a reward role")
    async def role_add(self, interaction: discord.Interaction, role: discord.Role):
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(
                "❌ That role is above my highest role — I can't manage it.", ephemeral=True
            )
        await self._upsert_cfg(interaction.guild_id)  # ensure config row exists
        await self.bot.db.execute(
            "INSERT INTO vanity_roles (guild_id, role_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            interaction.guild_id,
            role.id,
        )
        await self._refresh_cache()
        await interaction.response.send_message(f"✅ Added {role.mention} as a reward role.", ephemeral=True)

    @role_group.command(name="remove", description="Remove a reward role")
    async def role_remove(self, interaction: discord.Interaction, role: discord.Role):
        await self.bot.db.execute(
            "DELETE FROM vanity_roles WHERE guild_id=$1 AND role_id=$2",
            interaction.guild_id,
            role.id,
        )
        await self._refresh_cache()
        await interaction.response.send_message(f"✅ Removed {role.mention} from reward roles.", ephemeral=True)

    @role_group.command(name="list", description="List all reward roles")
    async def role_list(self, interaction: discord.Interaction):
        roles = self._roles.get(interaction.guild_id, set())
        if not roles:
            return await interaction.response.send_message("ℹ️ No reward roles configured.", ephemeral=True)
        mentions = []
        for rid in roles:
            r = interaction.guild.get_role(rid)
            mentions.append(r.mention if r else f"`<deleted role {rid}>`")
        await interaction.response.send_message("Reward roles: " + ", ".join(mentions), ephemeral=True)

    @vanity.command(name="resync", description="Force re-evaluation of every member in this server")
    async def vanity_resync(self, interaction: discord.Interaction):
        if not self._enabled_for(interaction.guild_id):
            return await interaction.response.send_message(
                "❌ Vanity is not fully configured (need substring/tag mode + at least one reward role).",
                ephemeral=True,
            )
        await interaction.response.send_message("⏳ Resync started…", ephemeral=True)
        count = 0
        for member in interaction.guild.members:
            if member.bot:
                continue
            try:
                await self._evaluate(member)
                count += 1
            except Exception:
                log.exception("resync failed for %s", member)
            await asyncio.sleep(0)
        await interaction.followup.send(f"✅ Resync complete — evaluated {count} members.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Vanity(bot))
