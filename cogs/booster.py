"""Booster auto-role.

When a member starts boosting (premium_since transitions from None → set), the
configured award role is granted. When they stop boosting, it's revoked.

Prefix-only commands.
"""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from utils.checks import is_guild_admin

log = logging.getLogger("sentinel.booster")

SCHEMA = """
CREATE TABLE IF NOT EXISTS booster_config (
    guild_id        BIGINT PRIMARY KEY,
    award_role_id   BIGINT
);
"""


class Booster(commands.Cog):
    """💎 Server booster auto-role"""

    def __init__(self, bot):
        self.bot = bot
        self._cache: dict[int, Optional[int]] = {}

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)
        rows = await self.bot.db.fetch("SELECT guild_id, award_role_id FROM booster_config")
        self._cache = {r["guild_id"]: r["award_role_id"] for r in rows}

    def _award_role(self, member: discord.Member) -> Optional[discord.Role]:
        rid = self._cache.get(member.guild.id)
        if rid is None:
            return None
        return member.guild.get_role(rid)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.premium_since is None and after.premium_since is not None:
            role = self._award_role(after)
            if role and role < after.guild.me.top_role and role not in after.roles:
                try:
                    await after.add_roles(role, reason="Booster award role")
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning("booster grant failed for %s: %s", after, e)
        elif before.premium_since is not None and after.premium_since is None:
            role = self._award_role(after)
            if role and role in after.roles:
                try:
                    await after.remove_roles(role, reason="Booster award role revoke")
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning("booster revoke failed for %s: %s", after, e)

    # ---------------- commands ----------------

    @commands.group(name="boosterrole", aliases=["br"], invoke_without_command=True)
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def boosterrole(self, ctx):
        """Booster role configuration."""
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        await ctx.send(
            f"💎 **Booster role**\n"
            f"`{prefix}boosterrole award set @role`\n"
            f"`{prefix}boosterrole award remove`\n"
            f"`{prefix}boosterrole award view`\n"
            f"`{prefix}boosterrole award sync`",
        )

    @boosterrole.group(name="award", invoke_without_command=True)
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def award(self, ctx):
        """Manage the booster award role."""
        await self.boosterrole(ctx)

    @award.command(name="set")
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def award_set(self, ctx, role: discord.Role):
        """Set the role granted automatically when a member boosts."""
        if role >= ctx.guild.me.top_role:
            return await ctx.send("❌ That role is above my highest role — I can't manage it.")
        await self.bot.db.execute(
            """INSERT INTO booster_config (guild_id, award_role_id) VALUES ($1, $2)
               ON CONFLICT (guild_id) DO UPDATE SET award_role_id = EXCLUDED.award_role_id""",
            ctx.guild.id, role.id,
        )
        self._cache[ctx.guild.id] = role.id
        await ctx.send(f"✅ Boosters will now receive {role.mention}.")

    @award.command(name="remove")
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def award_remove(self, ctx):
        """Stop auto-granting any role on boost."""
        await self.bot.db.execute(
            "UPDATE booster_config SET award_role_id = NULL WHERE guild_id = $1",
            ctx.guild.id,
        )
        self._cache[ctx.guild.id] = None
        await ctx.send("✅ Booster award role unset.")

    @award.command(name="view")
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def award_view(self, ctx):
        """View the current booster award role."""
        rid = self._cache.get(ctx.guild.id)
        role = ctx.guild.get_role(rid) if rid else None
        await ctx.send(f"Current booster award role: {role.mention if role else '—'}")

    @award.command(name="sync")
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def award_sync(self, ctx):
        """Grant/revoke the award role for current boosters now."""
        rid = self._cache.get(ctx.guild.id)
        if rid is None:
            return await ctx.send("❌ No award role configured.")
        role = ctx.guild.get_role(rid)
        if role is None or role >= ctx.guild.me.top_role:
            return await ctx.send("❌ Role missing or above my top role.")

        vanity_cog = self.bot.cogs.get("Vanity")
        msg = await ctx.send("⏳ Syncing boosters…")

        granted = revoked = skipped = 0
        for member in ctx.guild.members:
            if member.bot:
                continue
            try:
                if member.premium_since:
                    if role not in member.roles:
                        await member.add_roles(role, reason="Booster sync")
                        granted += 1
                else:
                    if role in member.roles:
                        if vanity_cog and vanity_cog._matches(member):
                            skipped += 1
                            continue
                        await member.remove_roles(role, reason="Booster sync (revoke)")
                        revoked += 1
            except (discord.Forbidden, discord.HTTPException):
                continue

        parts = []
        if granted:
            parts.append(f"+{granted} granted")
        if revoked:
            parts.append(f"-{revoked} revoked")
        if skipped:
            parts.append(f"{skipped} skipped (vanity)")
        summary = ", ".join(parts) if parts else "no changes"
        boosters = sum(1 for m in ctx.guild.members if not m.bot and m.premium_since)
        await msg.edit(content=f"**{boosters}** active booster(s) — {summary}.")


async def setup(bot):
    await bot.add_cog(Booster(bot))
