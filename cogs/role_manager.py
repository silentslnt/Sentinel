"""Role manager.

Per-member operations + bulk role-all (assign or remove a role across the guild
with rate limiting so Discord doesn't throttle us). Replaces the older `addrole`
/ `removerole` commands in moderation.py.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("sentinel.role_manager")

# 1.5s between API calls = ~40/min. Discord allows ~50/sec guildwide; this is
# conservative to coexist with other automations (vanity, booster, etc.).
BULK_DELAY = 1.5
PROGRESS_EVERY = 10  # update progress message every N members


class _BulkCancel(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=600)
        self.author_id = author_id
        self.cancelled = False

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Only the invoker can cancel.", ephemeral=True)
        self.cancelled = True
        button.disabled = True
        button.label = "Cancelling…"
        await interaction.response.edit_message(view=self)


def _hex_color(s: str) -> Optional[discord.Color]:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", s.strip())
    if m is None:
        return None
    return discord.Color(int(m.group(1), 16))


class RoleManager(commands.Cog):
    """🎭 Role manager"""

    def __init__(self, bot):
        self.bot = bot

    role = app_commands.Group(
        name="role",
        description="Role management",
        default_permissions=discord.Permissions(manage_roles=True),
        guild_only=True,
    )

    all_group = app_commands.Group(name="all", description="Bulk role operations", parent=role)

    # ---------- per-member ----------

    @role.command(name="add", description="Add a role to a member")
    async def add(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        if err := self._role_guard(interaction, role):
            return await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        if role in member.roles:
            return await interaction.response.send_message(f"ℹ️ {member.mention} already has {role.mention}.", ephemeral=True)
        try:
            await member.add_roles(role, reason=f"Added by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I don't have permission.", ephemeral=True)
        await interaction.response.send_message(f"✅ Added {role.mention} to {member.mention}.")

    @role.command(name="remove", description="Remove a role from a member")
    async def remove(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        if err := self._role_guard(interaction, role):
            return await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        if role not in member.roles:
            return await interaction.response.send_message(f"ℹ️ {member.mention} doesn't have {role.mention}.", ephemeral=True)
        try:
            await member.remove_roles(role, reason=f"Removed by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I don't have permission.", ephemeral=True)
        await interaction.response.send_message(f"✅ Removed {role.mention} from {member.mention}.")

    # ---------- create / edit / delete ----------

    @role.command(name="create", description="Create a new role")
    @app_commands.describe(name="Role name", color="Optional hex color, e.g. #5865F2")
    async def create(self, interaction: discord.Interaction, name: str, color: Optional[str] = None):
        kwargs = {"name": name[:100], "reason": f"Created by {interaction.user}"}
        if color:
            c = _hex_color(color)
            if c is None:
                return await interaction.response.send_message("❌ Color must be a 6-char hex like `#5865F2`.", ephemeral=True)
            kwargs["color"] = c
        try:
            new_role = await interaction.guild.create_role(**kwargs)
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I lack Manage Roles permission.", ephemeral=True)
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)
        await interaction.response.send_message(f"✅ Created {new_role.mention}.")

    @role.command(name="delete", description="Delete a role")
    async def delete(self, interaction: discord.Interaction, role: discord.Role):
        if err := self._role_guard(interaction, role):
            return await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        try:
            await role.delete(reason=f"Deleted by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I lack permission.", ephemeral=True)
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)
        await interaction.response.send_message(f"✅ Deleted role `{role.name}`.")

    @role.command(name="rename", description="Rename a role")
    async def rename(self, interaction: discord.Interaction, role: discord.Role, name: str):
        if err := self._role_guard(interaction, role):
            return await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        try:
            await role.edit(name=name[:100], reason=f"Renamed by {interaction.user}")
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)
        await interaction.response.send_message(f"✅ Renamed to {role.mention}.")

    @role.command(name="color", description="Change a role's color (hex)")
    async def color(self, interaction: discord.Interaction, role: discord.Role, hex_color: str):
        if err := self._role_guard(interaction, role):
            return await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        c = _hex_color(hex_color)
        if c is None:
            return await interaction.response.send_message("❌ Color must be a 6-char hex like `#5865F2`.", ephemeral=True)
        try:
            await role.edit(color=c, reason=f"Colored by {interaction.user}")
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)
        await interaction.response.send_message(f"✅ {role.mention} color updated.")

    @role.command(name="hoist", description="Toggle whether a role is shown separately in the member list")
    async def hoist(self, interaction: discord.Interaction, role: discord.Role):
        if err := self._role_guard(interaction, role):
            return await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        try:
            await role.edit(hoist=not role.hoist, reason=f"Hoist toggled by {interaction.user}")
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)
        state = "on" if not role.hoist else "off"
        await interaction.response.send_message(f"✅ {role.mention} hoist now **{state}**.")

    @role.command(name="mentionable", description="Toggle whether a role can be @-mentioned by anyone")
    async def mentionable(self, interaction: discord.Interaction, role: discord.Role):
        if err := self._role_guard(interaction, role):
            return await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        try:
            await role.edit(mentionable=not role.mentionable, reason=f"Mentionable toggled by {interaction.user}")
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)
        state = "on" if not role.mentionable else "off"
        await interaction.response.send_message(f"✅ {role.mention} mentionable now **{state}**.")

    # ---------- bulk ----------

    @all_group.command(name="add", description="Give a role to every human member")
    async def all_add(self, interaction: discord.Interaction, role: discord.Role):
        await self._bulk(interaction, role, action="add", target="humans")

    @all_group.command(name="addbots", description="Give a role to every bot member")
    async def all_addbots(self, interaction: discord.Interaction, role: discord.Role):
        await self._bulk(interaction, role, action="add", target="bots")

    @all_group.command(name="addin", description="Give a role to everyone who already has another role")
    @app_commands.describe(target_role="Role to give", source_role="Members must have this role")
    async def all_addin(self, interaction: discord.Interaction, target_role: discord.Role, source_role: discord.Role):
        await self._bulk(interaction, target_role, action="add", target="in", source_role=source_role)

    @all_group.command(name="remove", description="Remove a role from every member that has it")
    async def all_remove(self, interaction: discord.Interaction, role: discord.Role):
        await self._bulk(interaction, role, action="remove", target="all")

    async def _bulk(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        *,
        action: str,
        target: str,
        source_role: Optional[discord.Role] = None,
    ):
        if err := self._role_guard(interaction, role):
            return await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        if target == "in" and source_role is None:
            return await interaction.response.send_message("❌ Source role required.", ephemeral=True)

        guild = interaction.guild
        # Build the target list.
        if target == "humans":
            members = [m for m in guild.members if not m.bot and role not in m.roles]
            label = "humans without the role"
        elif target == "bots":
            members = [m for m in guild.members if m.bot and role not in m.roles]
            label = "bots without the role"
        elif target == "in":
            members = [m for m in guild.members if source_role in m.roles and role not in m.roles]
            label = f"members with {source_role.name}"
        else:  # all (remove)
            members = [m for m in guild.members if role in m.roles]
            label = "members with the role"

        if not members:
            return await interaction.response.send_message(
                f"ℹ️ Nothing to do — no {label}.", ephemeral=True,
            )

        eta_seconds = int(len(members) * BULK_DELAY)
        view = _BulkCancel(interaction.user.id)
        await interaction.response.send_message(
            f"⏳ {action.capitalize()}ing {role.mention} on {len(members)} member(s). "
            f"~{eta_seconds // 60}m {eta_seconds % 60}s. Updates every {PROGRESS_EVERY}.",
            view=view,
        )
        progress_msg = await interaction.original_response()

        done = failed = 0
        for i, member in enumerate(members, start=1):
            if view.cancelled:
                break
            try:
                if action == "add":
                    await member.add_roles(role, reason=f"Bulk by {interaction.user}")
                else:
                    await member.remove_roles(role, reason=f"Bulk by {interaction.user}")
                done += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
            if i % PROGRESS_EVERY == 0:
                try:
                    await progress_msg.edit(
                        content=f"⏳ {done}/{len(members)} done · {failed} failed · {len(members) - i} remaining…",
                        view=view,
                    )
                except discord.HTTPException:
                    pass
            await asyncio.sleep(BULK_DELAY)

        view.stop()
        try:
            final = (
                f"✅ Bulk {action} done. {done} succeeded, {failed} failed"
                + (" · cancelled" if view.cancelled else "") + "."
            )
            await progress_msg.edit(content=final, view=None)
        except discord.HTTPException:
            pass

    # ---------- helpers ----------

    @staticmethod
    def _role_guard(interaction: discord.Interaction, role: discord.Role) -> Optional[str]:
        guild = interaction.guild
        if role >= guild.me.top_role:
            return "That role is above my highest role — I can't manage it."
        if interaction.user != guild.owner and role >= interaction.user.top_role:
            return "That role is above your highest role."
        if role.is_default():
            return "That's the @everyone role; can't manage it that way."
        if role.managed:
            return "That role is managed by an integration and can't be edited."
        return None


async def setup(bot):
    await bot.add_cog(RoleManager(bot))
