"""Booster auto-role.

When a member starts boosting (premium_since transitions from None → set), the
configured award role is granted. When they stop boosting, it's revoked.
"""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

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
        # Boost gained.
        if before.premium_since is None and after.premium_since is not None:
            role = self._award_role(after)
            if role and role < after.guild.me.top_role and role not in after.roles:
                try:
                    await after.add_roles(role, reason="Booster award role")
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning("booster grant failed for %s: %s", after, e)
        # Boost lost.
        elif before.premium_since is not None and after.premium_since is None:
            role = self._award_role(after)
            if role and role in after.roles:
                try:
                    await after.remove_roles(role, reason="Booster award role revoke")
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning("booster revoke failed for %s: %s", after, e)

    boosterrole = app_commands.Group(
        name="boosterrole",
        description="Booster auto-role configuration",
        default_permissions=discord.Permissions(manage_roles=True),
        guild_only=True,
    )

    award = app_commands.Group(name="award", description="Manage the booster award role", parent=boosterrole)

    @award.command(name="set", description="Set the role granted automatically when a member boosts")
    async def award_set(self, interaction: discord.Interaction, role: discord.Role):
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(
                "❌ That role is above my highest role — I can't manage it.", ephemeral=True,
            )
        await self.bot.db.execute(
            """INSERT INTO booster_config (guild_id, award_role_id) VALUES ($1, $2)
               ON CONFLICT (guild_id) DO UPDATE SET award_role_id = EXCLUDED.award_role_id""",
            interaction.guild_id, role.id,
        )
        self._cache[interaction.guild_id] = role.id
        await interaction.response.send_message(f"✅ Boosters will now receive {role.mention}.", ephemeral=True)

    @award.command(name="remove", description="Stop auto-granting any role on boost")
    async def award_remove(self, interaction: discord.Interaction):
        await self.bot.db.execute(
            "UPDATE booster_config SET award_role_id = NULL WHERE guild_id = $1",
            interaction.guild_id,
        )
        self._cache[interaction.guild_id] = None
        await interaction.response.send_message("✅ Booster award role unset.", ephemeral=True)

    @award.command(name="view", description="View the current booster award role")
    async def award_view(self, interaction: discord.Interaction):
        rid = self._cache.get(interaction.guild_id)
        role = interaction.guild.get_role(rid) if rid else None
        await interaction.response.send_message(
            f"Current booster award role: {role.mention if role else '—'}", ephemeral=True,
        )

    @award.command(name="sync", description="Grant/revoke the award role for current boosters now")
    async def award_sync(self, interaction: discord.Interaction):
        rid = self._cache.get(interaction.guild_id)
        if rid is None:
            return await interaction.response.send_message("❌ No award role configured.", ephemeral=True)
        role = interaction.guild.get_role(rid)
        if role is None or role >= interaction.guild.me.top_role:
            return await interaction.response.send_message("❌ Role missing or above my top role.", ephemeral=True)

        await interaction.response.send_message("⏳ Syncing…", ephemeral=True)
        granted = revoked = 0
        for member in interaction.guild.members:
            try:
                if member.premium_since and role not in member.roles:
                    await member.add_roles(role, reason="Booster sync")
                    granted += 1
                elif not member.premium_since and role in member.roles:
                    await member.remove_roles(role, reason="Booster sync (revoke)")
                    revoked += 1
            except (discord.Forbidden, discord.HTTPException):
                continue
        await interaction.followup.send(
            f"✅ Synced — granted {granted}, revoked {revoked}.", ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(Booster(bot))
