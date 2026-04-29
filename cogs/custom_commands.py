"""User-defined custom commands.

A custom command (created via `/custom create <name>`) responds to:
  - prefix invocation:  `.<name>`
  - $ invocation:       `$<name>`
  - slash invocation:   `/run <name>`        (one slash command, name is an arg —
                                              we don't dynamically register slash
                                              commands per guild because Discord's
                                              global sync would rate-limit us.)

Each command has an action:
  - send_text     → send the configured text
  - send_embed    → send a saved embed by name (rendered with current ctx vars)
  - assign_role   → toggle the configured role on the invoker (ephemeral confirm)
"""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
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

ACTION_CHOICES = [
    app_commands.Choice(name="send_text",   value="send_text"),
    app_commands.Choice(name="send_embed",  value="send_embed"),
    app_commands.Choice(name="assign_role", value="assign_role"),
]


async def _execute(bot, command_row, *, channel, member, guild, send_func, ephemeral_send_func=None):
    action = command_row["action"]
    target = command_row["target"]

    if action == "send_text":
        text = embed_script.substitute_variables(target, user=member, guild=guild, channel=channel)
        await send_func(content=text)
    elif action == "send_embed":
        script = await fetch_script(bot, guild.id, target)
        if script is None:
            await send_func(content=f"❌ Embed `{target}` no longer exists.")
            return
        rendered = embed_script.render(script, user=member, guild=guild, channel=channel)
        view = await build_view(bot, guild.id, target)
        await send_func(
            content=rendered.content,
            embed=rendered.embed,
            view=view or discord.utils.MISSING,
        )
    elif action == "assign_role":
        try:
            role_id = int(target)
        except ValueError:
            await send_func(content="❌ Role target is invalid.")
            return
        role = guild.get_role(role_id)
        if role is None:
            await send_func(content="❌ That role no longer exists.")
            return
        if role >= guild.me.top_role:
            await send_func(content="❌ That role is above my highest role.")
            return
        try:
            if role in member.roles:
                await member.remove_roles(role, reason=f"Custom command")
                msg = f"✅ Removed {role.mention}."
            else:
                await member.add_roles(role, reason=f"Custom command")
                msg = f"✅ Added {role.mention}."
        except discord.Forbidden:
            msg = "❌ I don't have permission to manage that role."
        if ephemeral_send_func is not None:
            await ephemeral_send_func(content=msg)
        else:
            await send_func(content=msg)


class CustomCommands(commands.Cog):
    """🧩 Custom commands"""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)

    custom = app_commands.Group(
        name="custom",
        description="Custom commands",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @custom.command(name="create", description="Create a custom command")
    @app_commands.choices(action=ACTION_CHOICES)
    @app_commands.describe(
        name="Command name (used as .name and $name)",
        action="What this command does",
        target="For send_text: the text. For send_embed: embed name. For assign_role: role ID.",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        name: str,
        action: app_commands.Choice[str],
        target: str,
    ):
        name = name.lower()
        if not name.isalnum() and not all(c.isalnum() or c in "_-" for c in name):
            return await interaction.response.send_message(
                "❌ Name must be alphanumeric (with `_` or `-`).", ephemeral=True,
            )
        if action.value == "send_embed":
            if await fetch_script(self.bot, interaction.guild_id, target) is None:
                return await interaction.response.send_message(
                    f"❌ No saved embed named `{target}`.", ephemeral=True,
                )
        elif action.value == "assign_role":
            try:
                rid = int(target.strip("<@&>"))
            except ValueError:
                return await interaction.response.send_message(
                    "❌ Role target must be a role ID or mention.", ephemeral=True,
                )
            target = str(rid)
            if interaction.guild.get_role(rid) is None:
                return await interaction.response.send_message("❌ Role not found.", ephemeral=True)

        await self.bot.db.execute(
            """INSERT INTO custom_commands (guild_id, name, action, target)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (guild_id, name) DO UPDATE
               SET action = EXCLUDED.action, target = EXCLUDED.target""",
            interaction.guild_id, name, action.value, target,
        )
        await interaction.response.send_message(
            f"✅ Created `{name}` → {action.value}. Trigger with `.{name}`, `${name}`, or `/run name:{name}`.",
            ephemeral=True,
        )

    @custom.command(name="delete", description="Delete a custom command")
    async def delete(self, interaction: discord.Interaction, name: str):
        result = await self.bot.db.execute(
            "DELETE FROM custom_commands WHERE guild_id=$1 AND name=$2",
            interaction.guild_id, name.lower(),
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        if n == 0:
            return await interaction.response.send_message("❌ No such command.", ephemeral=True)
        await interaction.response.send_message(f"✅ Deleted `{name}`.", ephemeral=True)

    @custom.command(name="list", description="List custom commands")
    async def list_(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetch(
            "SELECT name, action, target FROM custom_commands WHERE guild_id=$1 ORDER BY name",
            interaction.guild_id,
        )
        if not rows:
            return await interaction.response.send_message("ℹ️ No custom commands.", ephemeral=True)
        lines = [f"`{r['name']}` → **{r['action']}** ({r['target'][:40]})" for r in rows[:50]]
        embed = discord.Embed(title="Custom Commands", description="\n".join(lines), color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="run", description="Run a custom command")
    @app_commands.guild_only()
    async def run_slash(self, interaction: discord.Interaction, name: str):
        row = await self.bot.db.fetchrow(
            "SELECT * FROM custom_commands WHERE guild_id=$1 AND name=$2",
            interaction.guild_id, name.lower(),
        )
        if row is None:
            return await interaction.response.send_message("❌ No such command.", ephemeral=True)
        # For slash, send first response ephemerally if assign_role; else publicly.
        if row["action"] == "assign_role":
            await interaction.response.defer(ephemeral=True)
            async def send(**kw):
                await interaction.followup.send(**kw, ephemeral=True)
            await _execute(
                self.bot, row,
                channel=interaction.channel, member=interaction.user, guild=interaction.guild,
                send_func=send, ephemeral_send_func=send,
            )
        else:
            await interaction.response.defer()
            async def send(**kw):
                await interaction.followup.send(**kw)
            await _execute(
                self.bot, row,
                channel=interaction.channel, member=interaction.user, guild=interaction.guild,
                send_func=send,
            )

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
            # Don't shadow real bot commands.
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

        async def send(**kw):
            await message.channel.send(**kw)

        await _execute(
            self.bot, row,
            channel=message.channel, member=message.author, guild=message.guild,
            send_func=send,
        )


async def setup(bot):
    await bot.add_cog(CustomCommands(bot))
