"""Help command — section-based two-level help."""
from __future__ import annotations

from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands


def _walk_app_commands(tree: app_commands.CommandTree) -> Iterable[app_commands.Command]:
    for cmd in tree.walk_commands():
        if isinstance(cmd, app_commands.Command):
            yield cmd


def _cog_label(cog_name: str, cog) -> str:
    raw = getattr(type(cog), "__cog_description__", "") if cog else ""
    label = raw.strip() or cog_name
    if label and label[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ":
        label = label.split(" ", 1)[-1] if " " in label else cog_name
    return label


class Help(commands.Cog):
    """Help command"""

    def __init__(self, bot):
        self.bot = bot

    def _prefix(self, ctx) -> str:
        return self.bot.guild_config.get_prefix(ctx.guild.id if ctx.guild else None)

    def _sections(self) -> dict[str, tuple[str, list[commands.Command]]]:
        """Returns {cog_name: (label, [commands])} for all non-empty, non-hidden cogs."""
        result = {}
        for cog_name, cog in self.bot.cogs.items():
            if cog_name == "Help":
                continue
            cmds = [c for c in cog.get_commands() if not c.hidden]
            if cmds:
                label = _cog_label(cog_name, cog)
                result[cog_name] = (label, sorted(cmds, key=lambda c: c.name))
        return result

    def _find_section(self, query: str) -> tuple[str, list[commands.Command]] | None:
        """Match a query against cog names and labels, case-insensitive."""
        q = query.lower()
        for cog_name, (label, cmds) in self._sections().items():
            if q in (cog_name.lower(), label.lower()):
                return label, cmds
        return None

    @commands.hybrid_command()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def help(self, ctx, *, query: str = None):
        """Show help sections, a section's commands, or a specific command."""
        prefix = self._prefix(ctx)

        if not query:
            return await self._overview(ctx, prefix)

        q = query.lower()

        # Try section match first
        section = self._find_section(q)
        if section:
            return await self._section_detail(ctx, section[0], section[1], prefix)

        # Try command match
        cmd = self.bot.get_command(q)
        if cmd:
            return await self._command_detail(ctx, cmd, prefix)

        # Try slash-only
        for app_cmd in _walk_app_commands(self.bot.tree):
            if app_cmd.qualified_name.lower() == q:
                return await self._slash_detail(ctx, app_cmd)

        await ctx.send(f"No command or section named `{query}` found.")

    async def _overview(self, ctx, prefix: str):
        sections = self._sections()
        total_cmds = sum(len(cmds) for _, cmds in sections.values())

        lines = []
        for _, (label, cmds) in sorted(sections.items()):
            lines.append(f"**{label}** — {len(cmds)} command{'s' if len(cmds) != 1 else ''}")

        embed = discord.Embed(
            title=self.bot.user.name,
            description="\n".join(lines),
            color=discord.Color.default(),
        )
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.set_footer(text=f"{total_cmds} commands across {len(sections)} sections · {prefix}help <section> or {prefix}help <command>")
        await ctx.send(embed=embed)

    async def _section_detail(self, ctx, label: str, cmds: list[commands.Command], prefix: str):
        lines = []
        for cmd in cmds:
            brief = (cmd.brief or (cmd.help or "").splitlines()[0])[:72] if (cmd.brief or cmd.help) else ""
            line = f"`{prefix}{cmd.name}`"
            if brief:
                line += f" — {brief}"
            lines.append(line)

        embed = discord.Embed(
            title=label,
            description="\n".join(lines),
            color=discord.Color.default(),
        )
        embed.set_footer(text=f"{len(cmds)} commands · {prefix}help <command> for usage")
        await ctx.send(embed=embed)

    async def _command_detail(self, ctx, cmd: commands.Command, prefix: str):
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
                    value="\n".join(
                        f"`{prefix}{cmd.name} {s.name}` — {(s.help or s.brief or 'No description.').splitlines()[0][:60]}"
                        for s in subs
                    ),
                    inline=False,
                )
        await ctx.send(embed=embed)

    async def _slash_detail(self, ctx, app_cmd: app_commands.Command):
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


async def setup(bot):
    await bot.add_cog(Help(bot))
