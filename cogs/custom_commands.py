"""User-defined custom commands. Prefix-only configuration.

A custom command (created via `.custom create <name>`) responds to:
  - prefix invocation:  `.<name>`
  - $ invocation:       `$<name>`

Each command has an action:
  - send_text     → send the configured text
  - send_embed    → send a saved embed by name (rendered with current ctx vars)
  - assign_role   → toggle the configured role on the invoker
"""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from utils import embed_script
from cogs.embeds import build_view, fetch_script

log = logging.getLogger("sentinel.custom_commands")

SCHEMA = """
CREATE TABLE IF NOT EXISTS custom_commands (
    guild_id BIGINT NOT NULL,
    name     TEXT   NOT NULL,
    action   TEXT   NOT NULL CHECK (action IN ('send_text','send_embed','assign_role')),
    target   TEXT   NOT NULL,
    PRIMARY KEY (guild_id, name)
);
"""

VALID_ACTIONS = ("send_text", "send_embed", "assign_role")


async def _execute(bot, command_row, *, channel, member, guild):
    action = command_row["action"]
    target = command_row["target"]

    if action == "send_text":
        text = embed_script.substitute_variables(target, user=member, guild=guild, channel=channel)
        await channel.send(content=text)
    elif action == "send_embed":
        script = await fetch_script(bot, guild.id, target)
        if script is None:
            await channel.send(f"❌ Embed `{target}` no longer exists.")
            return
        rendered = embed_script.render(script, user=member, guild=guild, channel=channel)
        view = await build_view(bot, guild.id, target)
        await channel.send(
            content=rendered.content,
            embed=rendered.embed,
            view=view or discord.utils.MISSING,
        )
    elif action == "assign_role":
        try:
            role_id = int(target)
        except ValueError:
            await channel.send("❌ Role target is invalid.")
            return
        role = guild.get_role(role_id)
        if role is None:
            await channel.send("❌ That role no longer exists.")
            return
        if role >= guild.me.top_role:
            await channel.send("❌ That role is above my highest role.")
            return
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Custom command")
                await channel.send(f"✅ Removed {role.mention} from {member.mention}.")
            else:
                await member.add_roles(role, reason="Custom command")
                await channel.send(f"✅ Added {role.mention} to {member.mention}.")
        except discord.Forbidden:
            await channel.send("❌ I don't have permission to manage that role.")


class CustomCommands(commands.Cog):
    """🧩 Custom commands"""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)

    @commands.group(name="custom", aliases=["cc"], invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def custom(self, ctx):
        """Custom commands."""
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        await ctx.send(
            f"🧩 **Custom commands** — actions: `send_text`, `send_embed`, `assign_role`\n"
            f"`{prefix}custom create <name> <action> <target>`\n"
            f"  · `send_text` target = the text\n"
            f"  · `send_embed` target = saved embed name\n"
            f"  · `assign_role` target = role ID or @role\n"
            f"`{prefix}custom delete <name>`\n"
            f"`{prefix}custom list`\n"
            f"Trigger commands with `{prefix}<name>` or `$<name>`.",
        )

    @custom.command(name="create")
    async def create(self, ctx, name: str, action: str, *, target: str):
        """Create a custom command."""
        name = name.lower()
        action = action.lower()
        if not all(c.isalnum() or c in "_-" for c in name):
            return await ctx.send("❌ Name must be alphanumeric (with `_` or `-`).")
        if action not in VALID_ACTIONS:
            return await ctx.send(f"❌ Action must be one of: {', '.join(VALID_ACTIONS)}")

        if action == "send_embed":
            if await fetch_script(self.bot, ctx.guild.id, target) is None:
                return await ctx.send(f"❌ No saved embed named `{target}`.")
        elif action == "assign_role":
            try:
                rid = int(target.strip("<@&>"))
            except ValueError:
                return await ctx.send("❌ Role target must be a role ID or mention.")
            target = str(rid)
            if ctx.guild.get_role(rid) is None:
                return await ctx.send("❌ Role not found.")

        await self.bot.db.execute(
            """INSERT INTO custom_commands (guild_id, name, action, target)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (guild_id, name) DO UPDATE
               SET action = EXCLUDED.action, target = EXCLUDED.target""",
            ctx.guild.id, name, action, target,
        )
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        await ctx.send(f"✅ Created `{name}` → {action}. Trigger with `{prefix}{name}` or `${name}`.")

    @custom.command(name="delete")
    async def delete(self, ctx, name: str):
        """Delete a custom command."""
        result = await self.bot.db.execute(
            "DELETE FROM custom_commands WHERE guild_id=$1 AND name=$2",
            ctx.guild.id, name.lower(),
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        if n == 0:
            return await ctx.send("❌ No such command.")
        await ctx.send(f"✅ Deleted `{name}`.")

    @custom.command(name="list")
    async def list_(self, ctx):
        """List custom commands."""
        rows = await self.bot.db.fetch(
            "SELECT name, action, target FROM custom_commands WHERE guild_id=$1 ORDER BY name",
            ctx.guild.id,
        )
        if not rows:
            return await ctx.send("ℹ️ No custom commands.")
        lines = [f"`{r['name']}` → **{r['action']}** ({r['target'][:40]})" for r in rows[:50]]
        embed = discord.Embed(title="Custom Commands", description="\n".join(lines), color=discord.Color.blurple())
        await ctx.send(embed=embed)

    # Listen for prefix-style and $-style invocations.
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        content = message.content.strip()
        if not content:
            return

        prefix = self.bot.guild_config.get_prefix(message.guild.id)
        name: Optional[str] = None
        if content.startswith("$"):
            name = content[1:].split()[0].lower()
        elif content.startswith(prefix):
            candidate = content[len(prefix):].split()[0].lower()
            if self.bot.get_command(candidate) is None:
                name = candidate

        if not name:
            return

        row = await self.bot.db.fetchrow(
            "SELECT * FROM custom_commands WHERE guild_id=$1 AND name=$2",
            message.guild.id, name,
        )
        if row is None:
            return

        await _execute(
            self.bot, row,
            channel=message.channel, member=message.author, guild=message.guild,
        )


async def setup(bot):
    await bot.add_cog(CustomCommands(bot))
