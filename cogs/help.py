"""Help command. Lists cogs and commands; resolves per-guild prefix."""
from __future__ import annotations

import discord
from discord.ext import commands


class Help(commands.Cog):
    """❓ Help command"""

    def __init__(self, bot):
        self.bot = bot

    def _prefix(self, ctx) -> str:
        return self.bot.guild_config.get_prefix(ctx.guild.id if ctx.guild else None)

    @commands.hybrid_command()
    async def help(self, ctx, *, command_name: str = None):
        """Show help for a command or list all categories."""
        prefix = self._prefix(ctx)

        if command_name:
            command = self.bot.get_command(command_name.lower())
            if not command:
                return await ctx.send(f"❌ Command `{command_name}` not found.")

            embed = discord.Embed(
                title=f"Help: {command.qualified_name}",
                description=command.help or "No description available",
                color=discord.Color.blurple(),
            )
            usage = f"{prefix}{command.qualified_name}"
            if command.signature:
                usage += f" {command.signature}"
            embed.add_field(name="Usage", value=f"`{usage}`", inline=False)
            if command.aliases:
                embed.add_field(
                    name="Aliases",
                    value=", ".join(f"`{a}`" for a in command.aliases),
                    inline=False,
                )
            return await ctx.send(embed=embed)

        embed = discord.Embed(
            title=f"{self.bot.user.name} — Help",
            description=f"Use `{prefix}help <command>` for details on a command.",
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)

        for cog_name, cog in self.bot.cogs.items():
            if cog_name == "Help":
                continue
            visible = [cmd.name for cmd in cog.get_commands() if not cmd.hidden]
            if not visible:
                continue
            embed.add_field(
                name=f"{cog.description or cog_name}",
                value=", ".join(f"`{n}`" for n in visible),
                inline=False,
            )

        embed.set_footer(text=f"Total commands: {len([c for c in self.bot.commands if not c.hidden])}")
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Help(bot))
