"""Help command — Bleed-style category embed with per-command descriptions."""
from __future__ import annotations

from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands


def _walk_app_commands(tree: app_commands.CommandTree) -> Iterable[app_commands.Command]:
    for cmd in tree.walk_commands():
        if isinstance(cmd, app_commands.Command):
            yield cmd


def _short(text: str | None, limit: int = 60) -> str:
    if not text:
        return "No description."
    first_line = text.strip().splitlines()[0]
    return first_line if len(first_line) <= limit else first_line[:limit - 1] + "…"


class Help(commands.Cog):
    """Help command"""

    def __init__(self, bot):
        self.bot = bot

    def _prefix(self, ctx) -> str:
        return self.bot.guild_config.get_prefix(ctx.guild.id if ctx.guild else None)

    @commands.hybrid_command()
    async def help(self, ctx, *, command_name: str = None):
        """Show help for a command or list all categories."""
        prefix = self._prefix(ctx)

        if command_name:
            return await self._command_detail(ctx, command_name.lower(), prefix)

        embed = discord.Embed(
            title=f"{self.bot.user.name}",
            description=f"Use `{prefix}help <command>` for detailed usage.",
            color=discord.Color.default(),
        )
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)

        # Group prefix commands by cog, skip hidden
        per_cog: dict[str, list[commands.Command]] = {}
        for cog_name, cog in self.bot.cogs.items():
            if cog_name == "Help":
                continue
            cmds = [c for c in cog.get_commands() if not c.hidden]
            if cmds:
                per_cog[cog_name] = sorted(cmds, key=lambda c: c.name)

        for cog_name in sorted(per_cog):
            cog = self.bot.get_cog(cog_name)
            # Use __cog_description__ directly — avoids shadowing by subcommands named "description"
            raw = getattr(type(cog), '__cog_description__', '') if cog else ''
            label = (raw.strip() or cog_name)
            # Strip leading emoji
            if label and label[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ":
                label = label.split(" ", 1)[-1] if " " in label else cog_name

            lines = []
            for cmd in per_cog[cog_name]:
                lines.append(f"`{prefix}{cmd.name}` — {_short(cmd.help or cmd.brief)}")
                # Show subcommands for groups
                if isinstance(cmd, commands.Group):
                    for sub in sorted(cmd.commands, key=lambda c: c.name):
                        if not sub.hidden:
                            lines.append(f"  `{prefix}{cmd.name} {sub.name}` — {_short(sub.help or sub.brief)}")

            if lines:
                embed.add_field(name=label, value="\n".join(lines), inline=False)

        total = sum(len(v) for v in per_cog.values())
        embed.set_footer(text=f"{total} commands · {prefix}help <command> for usage")
        await ctx.send(embed=embed)

    async def _command_detail(self, ctx, name: str, prefix: str):
        # Support "group sub" lookup
        cmd = self.bot.get_command(name)
        if cmd is not None:
            embed = discord.Embed(
                title=f"{prefix}{cmd.qualified_name}",
                description=cmd.help or "No description.",
                color=discord.Color.default(),
            )
            usage = f"{prefix}{cmd.qualified_name}"
            if cmd.signature:
                usage += f" {cmd.signature}"
            embed.add_field(name="Usage", value=f"`{usage}`", inline=False)
            if cmd.aliases:
                embed.add_field(name="Aliases", value=" · ".join(f"`{a}`" for a in cmd.aliases), inline=False)
            if isinstance(cmd, commands.Group):
                subs = sorted([c for c in cmd.commands if not c.hidden], key=lambda c: c.name)
                if subs:
                    embed.add_field(
                        name="Subcommands",
                        value="\n".join(f"`{prefix}{cmd.name} {s.name}` — {_short(s.help or s.brief)}" for s in subs),
                        inline=False,
                    )
            await ctx.send(embed=embed)
            return

        # Slash-only fallback
        for app_cmd in _walk_app_commands(self.bot.tree):
            if app_cmd.qualified_name.lower() == name:
                embed = discord.Embed(
                    title=f"/{app_cmd.qualified_name}",
                    description=app_cmd.description or "No description.",
                    color=discord.Color.default(),
                )
                params = " ".join(
                    f"<{p.name}>" if p.required else f"[{p.name}]"
                    for p in app_cmd.parameters
                )
                embed.add_field(name="Usage", value=f"`/{app_cmd.qualified_name} {params}`".strip(), inline=False)
                await ctx.send(embed=embed)
                return

        await ctx.send(f"No command named `{name}` found.")


async def setup(bot):
    await bot.add_cog(Help(bot))
