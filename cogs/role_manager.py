"""Role manager. Prefix-only.

Per-member operations + bulk role-all (assign or remove a role across the guild
with rate limiting so Discord doesn't throttle us).
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import discord
from discord.ext import commands

log = logging.getLogger("sentinel.role_manager")

BULK_DELAY = 1.5
PROGRESS_EVERY = 10


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

    @commands.group(name="role", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def role(self, ctx):
        """Role management."""
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        await ctx.send(
            f"🎭 **Role manager**\n"
            f"`{prefix}role add @user @role` · `{prefix}role remove @user @role`\n"
            f"`{prefix}role create <name> [hex]` · `{prefix}role delete @role`\n"
            f"`{prefix}role rename @role <name>` · `{prefix}role color @role <hex>`\n"
            f"`{prefix}role hoist @role` · `{prefix}role mentionable @role`\n"
            f"`{prefix}role all add @role` · `{prefix}role all addbots @role`\n"
            f"`{prefix}role all addin @target_role @source_role`\n"
            f"`{prefix}role all remove @role`",
        )

    # ---------- per-member ----------

    @role.command(name="add")
    async def add(self, ctx, member: discord.Member, role: discord.Role):
        """Add a role to a member."""
        if err := self._role_guard(ctx, role):
            return await ctx.send(f"❌ {err}")
        if role in member.roles:
            return await ctx.send(f"ℹ️ {member.mention} already has {role.mention}.")
        try:
            await member.add_roles(role, reason=f"Added by {ctx.author}")
        except discord.Forbidden:
            return await ctx.send("❌ I don't have permission.")
        await ctx.send(f"✅ Added {role.mention} to {member.mention}.")

    @role.command(name="remove")
    async def remove(self, ctx, member: discord.Member, role: discord.Role):
        """Remove a role from a member."""
        if err := self._role_guard(ctx, role):
            return await ctx.send(f"❌ {err}")
        if role not in member.roles:
            return await ctx.send(f"ℹ️ {member.mention} doesn't have {role.mention}.")
        try:
            await member.remove_roles(role, reason=f"Removed by {ctx.author}")
        except discord.Forbidden:
            return await ctx.send("❌ I don't have permission.")
        await ctx.send(f"✅ Removed {role.mention} from {member.mention}.")

    # ---------- create / edit / delete ----------

    @role.command(name="create")
    async def create(self, ctx, name: str, color: Optional[str] = None):
        """Create a new role. Optional hex color."""
        kwargs = {"name": name[:100], "reason": f"Created by {ctx.author}"}
        if color:
            c = _hex_color(color)
            if c is None:
                return await ctx.send("❌ Color must be a 6-char hex like `#5865F2`.")
            kwargs["color"] = c
        try:
            new_role = await ctx.guild.create_role(**kwargs)
        except discord.Forbidden:
            return await ctx.send("❌ I lack Manage Roles permission.")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Failed: {e}")
        await ctx.send(f"✅ Created {new_role.mention}.")

    @role.command(name="delete")
    async def delete(self, ctx, role: discord.Role):
        """Delete a role."""
        if err := self._role_guard(ctx, role):
            return await ctx.send(f"❌ {err}")
        try:
            await role.delete(reason=f"Deleted by {ctx.author}")
        except discord.Forbidden:
            return await ctx.send("❌ I lack permission.")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Failed: {e}")
        await ctx.send(f"✅ Deleted role `{role.name}`.")

    @role.command(name="rename")
    async def rename(self, ctx, role: discord.Role, *, name: str):
        """Rename a role."""
        if err := self._role_guard(ctx, role):
            return await ctx.send(f"❌ {err}")
        try:
            await role.edit(name=name[:100], reason=f"Renamed by {ctx.author}")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Failed: {e}")
        await ctx.send(f"✅ Renamed to {role.mention}.")

    @role.command(name="color")
    async def color(self, ctx, role: discord.Role, hex_color: str):
        """Change a role's color (hex)."""
        if err := self._role_guard(ctx, role):
            return await ctx.send(f"❌ {err}")
        c = _hex_color(hex_color)
        if c is None:
            return await ctx.send("❌ Color must be a 6-char hex like `#5865F2`.")
        try:
            await role.edit(color=c, reason=f"Colored by {ctx.author}")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Failed: {e}")
        await ctx.send(f"✅ {role.mention} color updated.")

    @role.command(name="hoist")
    async def hoist(self, ctx, role: discord.Role):
        """Toggle whether a role is shown separately in the member list."""
        if err := self._role_guard(ctx, role):
            return await ctx.send(f"❌ {err}")
        try:
            await role.edit(hoist=not role.hoist, reason=f"Hoist toggled by {ctx.author}")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Failed: {e}")
        state = "on" if not role.hoist else "off"
        await ctx.send(f"✅ {role.mention} hoist now **{state}**.")

    @role.command(name="mentionable")
    async def mentionable(self, ctx, role: discord.Role):
        """Toggle whether a role can be @-mentioned by anyone."""
        if err := self._role_guard(ctx, role):
            return await ctx.send(f"❌ {err}")
        try:
            await role.edit(mentionable=not role.mentionable, reason=f"Mentionable toggled by {ctx.author}")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Failed: {e}")
        state = "on" if not role.mentionable else "off"
        await ctx.send(f"✅ {role.mention} mentionable now **{state}**.")

    # ---------- bulk ----------

    @role.group(name="all", invoke_without_command=True)
    async def all_group(self, ctx):
        """Bulk role operations."""
        await self.role(ctx)

    @all_group.command(name="add")
    async def all_add(self, ctx, role: discord.Role):
        """Give a role to every human member."""
        await self._bulk(ctx, role, action="add", target="humans")

    @all_group.command(name="addbots")
    async def all_addbots(self, ctx, role: discord.Role):
        """Give a role to every bot member."""
        await self._bulk(ctx, role, action="add", target="bots")

    @all_group.command(name="addin")
    async def all_addin(self, ctx, target_role: discord.Role, source_role: discord.Role):
        """Give a role to everyone who already has another role."""
        await self._bulk(ctx, target_role, action="add", target="in", source_role=source_role)

    @all_group.command(name="remove")
    async def all_remove(self, ctx, role: discord.Role):
        """Remove a role from every member that has it."""
        await self._bulk(ctx, role, action="remove", target="all")

    async def _bulk(
        self,
        ctx,
        role: discord.Role,
        *,
        action: str,
        target: str,
        source_role: Optional[discord.Role] = None,
    ):
        if err := self._role_guard(ctx, role):
            return await ctx.send(f"❌ {err}")
        if target == "in" and source_role is None:
            return await ctx.send("❌ Source role required.")

        guild = ctx.guild
        if target == "humans":
            members = [m for m in guild.members if not m.bot and role not in m.roles]
            label = "humans without the role"
        elif target == "bots":
            members = [m for m in guild.members if m.bot and role not in m.roles]
            label = "bots without the role"
        elif target == "in":
            members = [m for m in guild.members if source_role in m.roles and role not in m.roles]
            label = f"members with {source_role.name}"
        else:
            members = [m for m in guild.members if role in m.roles]
            label = "members with the role"

        if not members:
            return await ctx.send(f"ℹ️ Nothing to do — no {label}.")

        eta_seconds = int(len(members) * BULK_DELAY)
        view = _BulkCancel(ctx.author.id)
        progress_msg = await ctx.send(
            f"⏳ {action.capitalize()}ing {role.mention} on {len(members)} member(s). "
            f"~{eta_seconds // 60}m {eta_seconds % 60}s. Updates every {PROGRESS_EVERY}.",
            view=view,
        )

        done = failed = 0
        for i, member in enumerate(members, start=1):
            if view.cancelled:
                break
            try:
                if action == "add":
                    await member.add_roles(role, reason=f"Bulk by {ctx.author}")
                else:
                    await member.remove_roles(role, reason=f"Bulk by {ctx.author}")
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
    def _role_guard(ctx, role: discord.Role) -> Optional[str]:
        guild = ctx.guild
        if role >= guild.me.top_role:
            return "That role is above my highest role — I can't manage it."
        if ctx.author != guild.owner and role >= ctx.author.top_role:
            return "That role is above your highest role."
        if role.is_default():
            return "That's the @everyone role; can't manage it that way."
        if role.managed:
            return "That role is managed by an integration and can't be edited."
        return None


async def setup(bot):
    await bot.add_cog(RoleManager(bot))
