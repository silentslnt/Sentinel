"""Invite tracking — who invited who, invite counts, manual adjustments.

Caches guild invites in memory. On member join, diffs the cache to find which
invite was used, stores it in DB, then dispatches on_member_join_tracked so
greet/welcome cogs get inviter info without a second API call.
"""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from utils.checks import is_guild_admin

log = logging.getLogger("sentinel.invites")

SCHEMA = """
CREATE TABLE IF NOT EXISTS invite_tracking (
    guild_id    BIGINT NOT NULL,
    inviter_id  BIGINT,
    invitee_id  BIGINT NOT NULL,
    invite_code TEXT   NOT NULL,
    PRIMARY KEY (guild_id, invitee_id)
);

CREATE TABLE IF NOT EXISTS invite_adjustments (
    guild_id BIGINT  NOT NULL,
    user_id  BIGINT  NOT NULL,
    fake     INTEGER NOT NULL DEFAULT 0,
    bonus    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
"""


class InviteTracker(commands.Cog):
    """Invite tracking"""

    def __init__(self, bot):
        self.bot = bot
        # guild_id -> {code: uses}
        self._cache: dict[int, dict[str, int]] = {}

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)
        for guild in self.bot.guilds:
            await self._cache_guild(guild)

    async def _cache_guild(self, guild: discord.Guild):
        try:
            invites = await guild.invites()
            self._cache[guild.id] = {inv.code: inv.uses for inv in invites}
        except (discord.Forbidden, discord.HTTPException):
            self._cache[guild.id] = {}

    # ---- listeners ----

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self._cache_guild(guild)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        self._cache.pop(guild.id, None)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        if invite.guild:
            self._cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses or 0

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        if invite.guild:
            self._cache.get(invite.guild.id, {}).pop(invite.code, None)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return

        inviter, code = await self._resolve(member)

        await self.bot.db.execute(
            """
            INSERT INTO invite_tracking (guild_id, inviter_id, invitee_id, invite_code)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id, invitee_id) DO UPDATE
                SET inviter_id  = EXCLUDED.inviter_id,
                    invite_code = EXCLUDED.invite_code
            """,
            member.guild.id,
            inviter.id if inviter else None,
            member.id,
            code or "unknown",
        )

        # Dispatch custom event so greet/welcome cogs get inviter without extra DB hit
        self.bot.dispatch("member_join_tracked", member, inviter, code)

    async def _resolve(
        self, member: discord.Member
    ) -> tuple[Optional[discord.User], Optional[str]]:
        guild = member.guild
        before = self._cache.get(guild.id, {})
        try:
            after_invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            return None, None

        inviter = None
        code = None
        for inv in after_invites:
            if inv.uses > before.get(inv.code, 0):
                inviter = inv.inviter
                code = inv.code
                break

        self._cache[guild.id] = {inv.code: inv.uses for inv in after_invites}
        return inviter, code

    # ---- helpers for other cogs ----

    async def get_invite_count(self, guild_id: int, user_id: int) -> tuple[int, int, int]:
        """Return (real, fake, bonus) invite counts."""
        real = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM invite_tracking WHERE guild_id=$1 AND inviter_id=$2",
            guild_id, user_id,
        ) or 0
        row = await self.bot.db.fetchrow(
            "SELECT fake, bonus FROM invite_adjustments WHERE guild_id=$1 AND user_id=$2",
            guild_id, user_id,
        )
        fake = row["fake"] if row else 0
        bonus = row["bonus"] if row else 0
        return int(real), int(fake), int(bonus)

    # ---- commands ----

    @commands.command(name="invites", aliases=["i"])
    @commands.guild_only()
    async def invites(self, ctx, member: discord.Member = None):
        """Show invite stats for a member."""
        target = member or ctx.author
        real, fake, bonus = await self.get_invite_count(ctx.guild.id, target.id)
        total = max(0, real - fake + bonus)
        embed = discord.Embed(
            description=(
                f"{target.mention} has **{total}** invite(s)\n"
                f"Regular: `{real}` · Fake: `{fake}` · Bonus: `{bonus}`"
            ),
            color=discord.Color.default(),
        )
        await ctx.send(embed=embed)

    @commands.command(name="inviter")
    @commands.guild_only()
    async def inviter_cmd(self, ctx, member: discord.Member = None):
        """Show who invited a member."""
        target = member or ctx.author
        row = await self.bot.db.fetchrow(
            "SELECT inviter_id, invite_code FROM invite_tracking WHERE guild_id=$1 AND invitee_id=$2",
            ctx.guild.id, target.id,
        )
        if not row or not row["inviter_id"]:
            return await ctx.send(f"No invite data found for {target.mention}.")
        mention = f"<@{row['inviter_id']}>"
        embed = discord.Embed(
            description=f"{target.mention} was invited by {mention} · code `{row['invite_code']}`",
            color=discord.Color.default(),
        )
        await ctx.send(embed=embed)

    @commands.command(name="invited")
    @commands.guild_only()
    async def invited(self, ctx, member: discord.Member = None):
        """Show who a member has invited."""
        target = member or ctx.author
        rows = await self.bot.db.fetch(
            "SELECT invitee_id FROM invite_tracking WHERE guild_id=$1 AND inviter_id=$2",
            ctx.guild.id, target.id,
        )
        if not rows:
            return await ctx.send(f"{target.mention} hasn't invited anyone.")
        mentions = " ".join(f"<@{r['invitee_id']}>" for r in rows[:20])
        extra = f"\n…and {len(rows) - 20} more" if len(rows) > 20 else ""
        embed = discord.Embed(
            title=f"Invited by {target.display_name}",
            description=mentions + extra,
            color=discord.Color.default(),
        )
        await ctx.send(embed=embed)

    @commands.command(name="inviteinfo", aliases=["invitecodes", "invitecode", "ic"])
    @commands.guild_only()
    async def inviteinfo(self, ctx, member: discord.Member = None):
        """Show active invite codes for a member."""
        target = member or ctx.author
        try:
            all_invites = await ctx.guild.invites()
        except discord.Forbidden:
            return await ctx.send("I don't have permission to view invites.")
        user_invites = [inv for inv in all_invites if inv.inviter and inv.inviter.id == target.id]
        if not user_invites:
            return await ctx.send(f"No active invite codes for {target.mention}.")
        lines = []
        for inv in user_invites[:10]:
            exp = f"<t:{int(inv.expires_at.timestamp())}:R>" if inv.expires_at else "never"
            lines.append(f"`{inv.code}` — {inv.uses} uses · expires {exp}")
        embed = discord.Embed(
            title=f"Invite codes — {target.display_name}",
            description="\n".join(lines),
            color=discord.Color.default(),
        )
        await ctx.send(embed=embed)

    @commands.command(name="addinvites")
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def addinvites(self, ctx, member: discord.Member, amount: int):
        """Add bonus invites to a member."""
        await self.bot.db.execute(
            """
            INSERT INTO invite_adjustments (guild_id, user_id, bonus)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id, user_id) DO UPDATE
                SET bonus = invite_adjustments.bonus + EXCLUDED.bonus
            """,
            ctx.guild.id, member.id, amount,
        )
        await ctx.send(f"Added `{amount}` invite(s) to {member.mention}.")

    @commands.command(name="removeinvites")
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def removeinvites(self, ctx, member: discord.Member, amount: int):
        """Remove invites from a member (adds to fake count)."""
        await self.bot.db.execute(
            """
            INSERT INTO invite_adjustments (guild_id, user_id, fake)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id, user_id) DO UPDATE
                SET fake = invite_adjustments.fake + EXCLUDED.fake
            """,
            ctx.guild.id, member.id, amount,
        )
        await ctx.send(f"Removed `{amount}` invite(s) from {member.mention}.")

    @commands.command(name="clearinvites", aliases=["resetinvites"])
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def clearinvites(self, ctx, *, target: str):
        """Reset invites. Usage: clearinvites all | clearinvites @member"""
        if target.lower() == "all":
            await self.bot.db.execute(
                "DELETE FROM invite_tracking WHERE guild_id=$1", ctx.guild.id
            )
            await self.bot.db.execute(
                "DELETE FROM invite_adjustments WHERE guild_id=$1", ctx.guild.id
            )
            return await ctx.send("Cleared all invite data for this server.")
        try:
            member = await commands.MemberConverter().convert(ctx, target)
        except commands.BadArgument:
            return await ctx.send("Provide `all` or a member mention.")
        await self.bot.db.execute(
            "DELETE FROM invite_tracking WHERE guild_id=$1 AND (invitee_id=$2 OR inviter_id=$2)",
            ctx.guild.id, member.id,
        )
        await self.bot.db.execute(
            "DELETE FROM invite_adjustments WHERE guild_id=$1 AND user_id=$2",
            ctx.guild.id, member.id,
        )
        await ctx.send(f"Cleared invite data for {member.mention}.")

    @commands.command(name="resetmyinvites", aliases=["rmi", "clearmyinvites"])
    @commands.guild_only()
    async def resetmyinvites(self, ctx):
        """Reset your own invite stats."""
        await self.bot.db.execute(
            "DELETE FROM invite_tracking WHERE guild_id=$1 AND inviter_id=$2",
            ctx.guild.id, ctx.author.id,
        )
        await self.bot.db.execute(
            "DELETE FROM invite_adjustments WHERE guild_id=$1 AND user_id=$2",
            ctx.guild.id, ctx.author.id,
        )
        await ctx.send("Your invite stats have been reset.")


async def setup(bot):
    await bot.add_cog(InviteTracker(bot))
