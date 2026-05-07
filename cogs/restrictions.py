"""Command restriction, fake permission, and disable system. Bleed-style.

restrict <command> [role]   — lock command to a role (omit role to remove)
fp add/remove/list          — grant/revoke fake Discord permissions to a role
disable/enable <command>    — toggle a command in this guild
disabled                    — list all disabled commands

All gated to is_whitelisted (server owner, OWNER_ID, or explicit whitelist).

The global check (_global_check) is registered on the bot so it runs before
every command — no per-command wiring needed.
"""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from utils.checks import is_whitelisted

log = logging.getLogger("sentinel.restrictions")

VALID_PERMISSIONS = {
    "administrator", "ban_members", "kick_members",
    "manage_guild", "manage_messages", "manage_channels",
    "manage_roles", "moderate_members", "manage_webhooks",
    "manage_emojis_and_stickers", "view_audit_log",
    "mention_everyone", "manage_nicknames", "manage_threads",
}

# Commands that can never be disabled or restricted.
PROTECTED = {
    "restrict", "fp", "fakepermission", "disable", "enable", "disabled",
    "admin", "reload", "sync", "shutdown",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS disabled_commands (
    guild_id BIGINT NOT NULL,
    command  TEXT   NOT NULL,
    PRIMARY KEY (guild_id, command)
);

CREATE TABLE IF NOT EXISTS command_restrictions (
    guild_id BIGINT NOT NULL,
    command  TEXT   NOT NULL,
    role_id  BIGINT NOT NULL,
    PRIMARY KEY (guild_id, command)
);

CREATE TABLE IF NOT EXISTS fake_permissions (
    guild_id   BIGINT NOT NULL,
    role_id    BIGINT NOT NULL,
    permission TEXT   NOT NULL,
    PRIMARY KEY (guild_id, role_id, permission)
);
"""


class Restrictions(commands.Cog):
    """🔒 Restrict / disable / fake permissions"""

    def __init__(self, bot):
        self.bot = bot
        # guild_id -> set of disabled command qualified names
        self._disabled: dict[int, set[str]] = {}
        # guild_id -> {qualified_name -> role_id}
        self._restrictions: dict[int, dict[str, int]] = {}
        # guild_id -> {role_id -> set of permission strings}
        self._fake_perms: dict[int, dict[int, set[str]]] = {}

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)
        await self._refresh()
        self.bot.add_check(self._global_check)

    async def cog_unload(self):
        self.bot.remove_check(self._global_check)

    async def _refresh(self):
        rows = await self.bot.db.fetch("SELECT guild_id, command FROM disabled_commands")
        self._disabled = {}
        for r in rows:
            self._disabled.setdefault(r["guild_id"], set()).add(r["command"])

        rows = await self.bot.db.fetch("SELECT guild_id, command, role_id FROM command_restrictions")
        self._restrictions = {}
        for r in rows:
            self._restrictions.setdefault(r["guild_id"], {})[r["command"]] = r["role_id"]

        rows = await self.bot.db.fetch("SELECT guild_id, role_id, permission FROM fake_permissions")
        self._fake_perms = {}
        for r in rows:
            self._fake_perms.setdefault(r["guild_id"], {}).setdefault(r["role_id"], set()).add(r["permission"])

    # ------------------------------------------------------------------ #
    # Global check — runs before every command invocation                 #
    # ------------------------------------------------------------------ #

    async def _global_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None or ctx.command is None:
            return True
        # Server owner and bot owner bypass everything
        if ctx.author.id == ctx.guild.owner_id:
            return True
        if self.bot.owner_id and ctx.author.id == self.bot.owner_id:
            return True

        cmd = ctx.command.qualified_name

        # Disabled check
        if cmd in self._disabled.get(ctx.guild.id, set()):
            raise commands.CheckFailure("That command is disabled in this server.")

        # Role restriction check
        role_id = self._restrictions.get(ctx.guild.id, {}).get(cmd)
        if role_id is not None:
            role = ctx.guild.get_role(role_id)
            if role is None or role not in ctx.author.roles:
                label = role.mention if role else "a deleted role"
                raise commands.CheckFailure(f"This command is restricted to {label}.")

        return True

    # ------------------------------------------------------------------ #
    # restrict                                                            #
    # ------------------------------------------------------------------ #

    @commands.command(name="restrict", aliases=["rc"])
    @commands.guild_only()
    @commands.check(is_whitelisted)
    async def restrict(self, ctx, command: str, role: Optional[discord.Role] = None):
        """Restrict a command to a role, or pass no role to remove the restriction."""
        cmd = self.bot.get_command(command)
        if cmd is None:
            return await ctx.send(f"❌ Unknown command `{command}`.")
        if cmd.qualified_name in PROTECTED:
            return await ctx.send("❌ That command cannot be restricted.")

        if role is None:
            await self.bot.db.execute(
                "DELETE FROM command_restrictions WHERE guild_id=$1 AND command=$2",
                ctx.guild.id, cmd.qualified_name,
            )
            self._restrictions.get(ctx.guild.id, {}).pop(cmd.qualified_name, None)
            return await ctx.send(f"✅ Restriction removed from `{cmd.qualified_name}`.")

        await self.bot.db.execute(
            """INSERT INTO command_restrictions (guild_id, command, role_id) VALUES ($1,$2,$3)
               ON CONFLICT (guild_id, command) DO UPDATE SET role_id=EXCLUDED.role_id""",
            ctx.guild.id, cmd.qualified_name, role.id,
        )
        self._restrictions.setdefault(ctx.guild.id, {})[cmd.qualified_name] = role.id
        await ctx.send(f"✅ `{cmd.qualified_name}` is now restricted to {role.mention}.")

    @commands.command(name="restrictions")
    @commands.guild_only()
    @commands.check(is_whitelisted)
    async def restrictions_list(self, ctx):
        """List all command restrictions in this server."""
        rows = self._restrictions.get(ctx.guild.id, {})
        if not rows:
            return await ctx.send("ℹ️ No restrictions configured.")
        lines = []
        for cmd, role_id in sorted(rows.items()):
            role = ctx.guild.get_role(role_id)
            lines.append(f"`{cmd}` → {role.mention if role else f'<deleted {role_id}>'}")
        await ctx.send("\n".join(lines))

    # ------------------------------------------------------------------ #
    # fp — fake permissions                                               #
    # ------------------------------------------------------------------ #

    @commands.group(name="fakepermission", aliases=["fp"], invoke_without_command=True)
    @commands.guild_only()
    @commands.check(is_whitelisted)
    async def fp(self, ctx):
        """Fake permission system — lets a role pass permission checks without real perms."""
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        perms = " ".join(f"`{p}`" for p in sorted(VALID_PERMISSIONS))
        await ctx.send(
            f"**Fake permissions**\n"
            f"`{prefix}fp add @role <permission>`\n"
            f"`{prefix}fp remove @role <permission>`\n"
            f"`{prefix}fp list [@role]`\n"
            f"Valid permissions: {perms}",
        )

    @fp.command(name="add")
    async def fp_add(self, ctx, role: discord.Role, permission: str):
        """Grant a role a fake Discord permission."""
        permission = permission.lower()
        if permission not in VALID_PERMISSIONS:
            return await ctx.send(
                f"❌ Invalid permission. Valid: {', '.join(sorted(VALID_PERMISSIONS))}"
            )
        await self.bot.db.execute(
            "INSERT INTO fake_permissions (guild_id, role_id, permission) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
            ctx.guild.id, role.id, permission,
        )
        self._fake_perms.setdefault(ctx.guild.id, {}).setdefault(role.id, set()).add(permission)
        await ctx.send(f"✅ {role.mention} now has fake `{permission}`.")

    @fp.command(name="remove")
    async def fp_remove(self, ctx, role: discord.Role, permission: str):
        """Remove a fake permission from a role."""
        permission = permission.lower()
        await self.bot.db.execute(
            "DELETE FROM fake_permissions WHERE guild_id=$1 AND role_id=$2 AND permission=$3",
            ctx.guild.id, role.id, permission,
        )
        self._fake_perms.get(ctx.guild.id, {}).get(role.id, set()).discard(permission)
        await ctx.send(f"✅ Removed fake `{permission}` from {role.mention}.")

    @fp.command(name="list")
    async def fp_list(self, ctx, role: Optional[discord.Role] = None):
        """List fake permissions, optionally filtered by a specific role."""
        guild_fps = self._fake_perms.get(ctx.guild.id, {})
        if not guild_fps:
            return await ctx.send("ℹ️ No fake permissions configured.")
        lines = []
        for role_id, perms in guild_fps.items():
            if role and role_id != role.id:
                continue
            r = ctx.guild.get_role(role_id)
            label = r.mention if r else f"`<deleted {role_id}>`"
            lines.append(f"{label}: {', '.join(f'`{p}`' for p in sorted(perms))}")
        if not lines:
            return await ctx.send("ℹ️ No fake permissions for that role.")
        await ctx.send("\n".join(lines))

    # ------------------------------------------------------------------ #
    # disable / enable                                                    #
    # ------------------------------------------------------------------ #

    @commands.command(name="disable")
    @commands.guild_only()
    @commands.check(is_whitelisted)
    async def disable(self, ctx, *, command: str):
        """Disable a command in this server."""
        cmd = self.bot.get_command(command)
        if cmd is None:
            return await ctx.send(f"❌ Unknown command `{command}`.")
        if cmd.qualified_name in PROTECTED:
            return await ctx.send("❌ That command cannot be disabled.")
        await self.bot.db.execute(
            "INSERT INTO disabled_commands (guild_id, command) VALUES ($1,$2) ON CONFLICT DO NOTHING",
            ctx.guild.id, cmd.qualified_name,
        )
        self._disabled.setdefault(ctx.guild.id, set()).add(cmd.qualified_name)
        await ctx.send(f"✅ `{cmd.qualified_name}` disabled in this server.")

    @commands.command(name="enable")
    @commands.guild_only()
    @commands.check(is_whitelisted)
    async def enable(self, ctx, *, command: str):
        """Re-enable a disabled command."""
        cmd = self.bot.get_command(command)
        name = cmd.qualified_name if cmd else command
        await self.bot.db.execute(
            "DELETE FROM disabled_commands WHERE guild_id=$1 AND command=$2",
            ctx.guild.id, name,
        )
        self._disabled.get(ctx.guild.id, set()).discard(name)
        await ctx.send(f"✅ `{name}` enabled.")

    @commands.command(name="disabled")
    @commands.guild_only()
    @commands.check(is_whitelisted)
    async def disabled_list(self, ctx):
        """List all disabled commands in this server."""
        cmds = self._disabled.get(ctx.guild.id, set())
        if not cmds:
            return await ctx.send("ℹ️ No commands are disabled.")
        await ctx.send("Disabled: " + ", ".join(f"`{c}`" for c in sorted(cmds)))


async def setup(bot):
    await bot.add_cog(Restrictions(bot))
