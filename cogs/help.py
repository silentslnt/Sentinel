"""Help command — paginated category view with arrow navigation."""
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


class HelpPaginator(discord.ui.View):
    def __init__(self, pages: list[discord.Embed], author_id: int):
        super().__init__(timeout=120)
        self.pages = pages
        self.index = 0
        self.author_id = author_id
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.index == 0
        self.next_btn.disabled = self.index >= len(self.pages) - 1

    async def _goto(self, interaction: discord.Interaction, delta: int):
        if interaction.user.id != self.author_id:
            return await interaction.response.defer()
        self.index += delta
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._goto(interaction, -1)

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._goto(interaction, 1)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class Help(commands.Cog):
    """Help command"""

    def __init__(self, bot):
        self.bot = bot

    def _prefix(self, ctx) -> str:
        return self.bot.guild_config.get_prefix(ctx.guild.id if ctx.guild else None)

    def _build_pages(self, prefix: str) -> list[discord.Embed]:
        per_cog: dict[str, list[commands.Command]] = {}
        for cog_name, cog in self.bot.cogs.items():
            if cog_name == "Help":
                continue
            cmds = [c for c in cog.get_commands() if not c.hidden]
            if cmds:
                per_cog[cog_name] = sorted(cmds, key=lambda c: c.name)

        total = sum(len(v) for v in per_cog.values())
        sorted_cogs = sorted(per_cog)
        num_pages = len(sorted_cogs) + 1  # overview + one per cog

        # Page 0: overview
        overview_lines = []
        for cog_name in sorted_cogs:
            cog = self.bot.get_cog(cog_name)
            label = _cog_label(cog_name, cog)
            names = "  ".join(f"`{c.name}`" for c in per_cog[cog_name])
            overview_lines.append(f"**{label}**\n{names}")

        overview = discord.Embed(
            title=self.bot.user.name,
            description="\n\n".join(overview_lines),
            color=discord.Color.default(),
        )
        overview.set_thumbnail(url=self.bot.user.display_avatar.url)
        overview.set_footer(text=f"{total} commands · {prefix}help <command> for usage · page 1/{num_pages}")
        pages = [overview]

        # One page per cog
        for i, cog_name in enumerate(sorted_cogs, 2):
            cog = self.bot.get_cog(cog_name)
            label = _cog_label(cog_name, cog)
            lines = []
            for cmd in per_cog[cog_name]:
                desc = (cmd.brief or cmd.help or "").splitlines()[0][:80] if (cmd.brief or cmd.help) else ""
                line = f"`{prefix}{cmd.name}`"
                if cmd.aliases:
                    line += f" · `{'` `'.join(cmd.aliases)}`"
                if desc:
                    line += f"\n{desc}"
                lines.append(line)
            embed = discord.Embed(
                title=label,
                description="\n\n".join(lines) or "No commands.",
                color=discord.Color.default(),
            )
            embed.set_footer(text=f"{len(per_cog[cog_name])} commands · page {i}/{num_pages}")
            pages.append(embed)

        return pages

    @commands.hybrid_command()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def help(self, ctx, *, command_name: str = None):
        """Show help for a command or list all categories."""
        prefix = self._prefix(ctx)

        if command_name:
            return await self._command_detail(ctx, command_name.lower(), prefix)

        pages = self._build_pages(prefix)
        view = HelpPaginator(pages, ctx.author.id)
        await ctx.send(embed=pages[0], view=view)

    async def _command_detail(self, ctx, name: str, prefix: str):
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
                        value="\n".join(
                            f"`{prefix}{cmd.name} {s.name}` — {(s.help or s.brief or 'No description.').splitlines()[0][:60]}"
                            for s in subs
                        ),
                        inline=False,
                    )
            await ctx.send(embed=embed)
            return

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
