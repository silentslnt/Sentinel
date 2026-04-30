"""Help command. Lists every command in the bot — hybrid, prefix, and slash-only."""
from __future__ import annotations

from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands


def _walk_app_commands(tree: app_commands.CommandTree) -> Iterable[app_commands.Command]:
    """Yield every leaf app-command in the tree (descending into groups)."""
    for cmd in tree.walk_commands():
        if isinstance(cmd, app_commands.Command):
            yield cmd


class Help(commands.Cog):
    """❓ Help command"""

    def __init__(self, bot):
        self.bot = bot

    def _prefix(self, ctx) -> str:
        return self.bot.guild_config.get_prefix(ctx.guild.id if ctx.guild else None)

    def _all_command_names(self) -> set[str]:
        names: set[str] = set()
        for c in self.bot.commands:
            if not c.hidden:
                names.add(c.qualified_name)
        for c in _walk_app_commands(self.bot.tree):
            names.add(c.qualified_name)
        return names

    @commands.hybrid_command()
    async def help(self, ctx, *, command_name: str = None):
        """Show help for a command or list all categories."""
        prefix = self._prefix(ctx)

        if command_name:
            return await self._command_detail(ctx, command_name.lower(), prefix)

        embed = discord.Embed(
            title=f"{self.bot.user.name} — Help",
            description=(
                f"Use `{prefix}help <command>` for details.\n"
                f"Slash commands are also available — start typing `/` in any channel."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)

        # Group by cog. Build a name set per cog covering both prefix and slash commands.
        per_cog: dict[str, set[str]] = {}
        for cog_name, cog in self.bot.cogs.items():
            if cog_name == "Help":
                continue
            for cmd in cog.get_commands():
                if not cmd.hidden:
                    per_cog.setdefault(cog_name, set()).add(cmd.name)
            # App-commands are attached to the tree, not the cog; map by binding.
            for app_cmd in _walk_app_commands(self.bot.tree):
                if getattr(app_cmd, "binding", None) is cog:
                    per_cog.setdefault(cog_name, set()).add(app_cmd.qualified_name)

        for cog_name in sorted(per_cog):
            cog = self.bot.get_cog(cog_name)
            heading = cog.description or cog_name if cog else cog_name
            cmds = sorted(per_cog[cog_name])
            embed.add_field(
                name=heading,
                value=", ".join(f"`{n}`" for n in cmds),
                inline=False,
            )

        embed.set_footer(text=f"Total commands: {len(self._all_command_names())}")
        await ctx.send(embed=embed)

    async def _command_detail(self, ctx, name: str, prefix: str):
        # Prefix/hybrid path
        cmd = self.bot.get_command(name)
        if cmd is not None:
            embed = discord.Embed(
                title=f"Help: {cmd.qualified_name}",
                description=cmd.help or "No description available",
                color=discord.Color.blurple(),
            )
            usage = f"{prefix}{cmd.qualified_name}"
            if cmd.signature:
                usage += f" {cmd.signature}"
            embed.add_field(name="Usage", value=f"`{usage}`", inline=False)
            if cmd.aliases:
                embed.add_field(name="Aliases", value=", ".join(f"`{a}`" for a in cmd.aliases), inline=False)
            return await ctx.send(embed=embed)

        # Slash-only path
        for app_cmd in _walk_app_commands(self.bot.tree):
            if app_cmd.qualified_name.lower() == name:
                embed = discord.Embed(
                    title=f"Help: /{app_cmd.qualified_name}",
                    description=app_cmd.description or "No description available",
                    color=discord.Color.blurple(),
                )
                params = " ".join(
                    f"<{p.name}>" if p.required else f"[{p.name}]"
                    for p in app_cmd.parameters
                )
                embed.add_field(name="Usage", value=f"`/{app_cmd.qualified_name} {params}`".strip(), inline=False)
                return await ctx.send(embed=embed)

        await ctx.send(f"❌ Command `{name}` not found.")


async def setup(bot):
    await bot.add_cog(Help(bot))
