"""Moderation commands. Hybrid (slash + prefix)."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Optional

import discord
from discord.ext import commands

from utils.checks import is_whitelisted, with_perms

MAX_TIMEOUT_MINUTES = 40320  # Discord cap: 28 days
MAX_SLOWMODE_SECONDS = 21600  # Discord cap: 6 hours


class ConfirmView(discord.ui.View):
    """Two-button confirm/cancel view scoped to a single user."""

    def __init__(self, author_id: int, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.value: Optional[bool] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the command invoker can confirm this.", ephemeral=True
            )
            return False
        return True

    async def _finalize(self, interaction: discord.Interaction, value: bool):
        self.value = value
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finalize(interaction, True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finalize(interaction, False)


class Moderation(commands.Cog):
    """🛡️ Moderation commands to manage your server"""

    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def _hierarchy_error(ctx: commands.Context, target: discord.Member) -> Optional[str]:
        if target == ctx.author:
            return "You can't target yourself."
        if target == ctx.guild.owner:
            return "You can't target the server owner."
        if target.id == ctx.bot.user.id:
            return "I can't target myself."
        if ctx.author != ctx.guild.owner and target.top_role >= ctx.author.top_role:
            return "Your highest role must be above the target's highest role."
        if target.top_role >= ctx.guild.me.top_role:
            return "My highest role must be above the target's highest role."
        return None

    @commands.command()
    @commands.guild_only()
    @with_perms(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    async def kick(self, ctx, member: discord.Member, *, reason: Optional[str] = None):
        """Kick a member from the server."""
        if err := self._hierarchy_error(ctx, member):
            return await ctx.send(f"❌ {err}")
        try:
            await member.kick(reason=f"{ctx.author} ({ctx.author.id}): {reason or 'No reason'}")
        except discord.Forbidden:
            return await ctx.send("❌ I don't have permission to kick this member.")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Failed to kick: {e}")

        embed = discord.Embed(
            title="👢 Member Kicked",
            description=f"{member.mention} has been kicked",
            color=discord.Color.orange(),
        )
        if reason:
            embed.add_field(name="Reason", value=reason)
        embed.set_footer(text=f"Kicked by {ctx.author}")
        await ctx.send(embed=embed)

    @commands.command()
    @commands.guild_only()
    @with_perms(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def ban(self, ctx, member: discord.Member, *, reason: Optional[str] = None):
        """Ban a member from the server."""
        if err := self._hierarchy_error(ctx, member):
            return await ctx.send(f"❌ {err}")
        try:
            await member.ban(reason=f"{ctx.author} ({ctx.author.id}): {reason or 'No reason'}")
        except discord.Forbidden:
            return await ctx.send("❌ I don't have permission to ban this member.")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Failed to ban: {e}")

        embed = discord.Embed(
            title="🔨 Member Banned",
            description=f"{member.mention} has been banned",
            color=discord.Color.red(),
        )
        if reason:
            embed.add_field(name="Reason", value=reason)
        embed.set_footer(text=f"Banned by {ctx.author}")
        await ctx.send(embed=embed)

    @commands.command()
    @commands.guild_only()
    @with_perms(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def unban(self, ctx, *, user: str):
        """Unban a user. Accepts a user ID (preferred) or username."""
        target: Optional[discord.User] = None

        if user.isdigit():
            try:
                entry = await ctx.guild.fetch_ban(discord.Object(id=int(user)))
                target = entry.user
            except discord.NotFound:
                return await ctx.send("❌ That user is not banned.")
            except discord.HTTPException as e:
                return await ctx.send(f"❌ Failed to look up ban: {e}")
        else:
            name = user.split("#", 1)[0].lower()
            async for entry in ctx.guild.bans():
                if entry.user.name.lower() == name or str(entry.user).lower() == user.lower():
                    target = entry.user
                    break
            if target is None:
                return await ctx.send(
                    f"❌ No banned user matched `{user}`. Try the user ID instead."
                )

        try:
            await ctx.guild.unban(target, reason=f"Unbanned by {ctx.author}")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Failed to unban: {e}")
        await ctx.send(f"✅ Unbanned **{target}** (`{target.id}`)")

    @commands.command(aliases=["m", "timeout"])
    @commands.guild_only()
    @with_perms(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def mute(
        self,
        ctx,
        member: discord.Member,
        duration: int = 60,
        *,
        reason: Optional[str] = None,
    ):
        """Timeout a member. Duration in minutes (max 40320 = 28 days)."""
        if err := self._hierarchy_error(ctx, member):
            return await ctx.send(f"❌ {err}")
        if duration < 1 or duration > MAX_TIMEOUT_MINUTES:
            return await ctx.send(
                f"❌ Duration must be between 1 and {MAX_TIMEOUT_MINUTES} minutes (28 days)."
            )
        try:
            await member.timeout(timedelta(minutes=duration), reason=reason)
        except discord.Forbidden:
            return await ctx.send("❌ I don't have permission to timeout this member.")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Failed to timeout: {e}")

        embed = discord.Embed(
            title="🔇 Member Muted",
            description=f"{member.mention} has been muted for {duration} minutes",
            color=discord.Color.dark_gray(),
        )
        if reason:
            embed.add_field(name="Reason", value=reason)
        await ctx.send(embed=embed)

    @commands.command(aliases=["um", "untimeout"])
    @commands.guild_only()
    @with_perms(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def unmute(self, ctx, member: discord.Member):
        """Remove timeout from a member."""
        if member.timed_out_until is None:
            return await ctx.send(f"ℹ️ {member.mention} is not muted.")
        try:
            await member.timeout(None, reason=f"Unmuted by {ctx.author}")
        except discord.Forbidden:
            return await ctx.send("❌ I don't have permission to remove this timeout.")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Failed to unmute: {e}")
        await ctx.send(f"🔊 {member.mention} has been unmuted")

    @commands.command(aliases=["clear", "c", "pg"])
    @commands.guild_only()
    @with_perms(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def purge(self, ctx, amount: int):
        """Purge messages from the channel. Amount (1–100) is required."""
        if amount < 1 or amount > 100:
            return await ctx.send("❌ Please provide a number between 1 and 100")

        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
            deleted = await ctx.channel.purge(limit=amount)
            return await ctx.send(f"Cleared {len(deleted)} messages", ephemeral=True)

        deleted = await ctx.channel.purge(limit=amount + 1)
        msg = await ctx.send(f"Cleared {len(deleted) - 1} messages")
        await asyncio.sleep(3)
        try:
            await msg.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

    @commands.command(aliases=["sm"])
    @commands.guild_only()
    @with_perms(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def slowmode(self, ctx, seconds: int = 0):
        """Set channel slowmode (0 to disable, max 21600 = 6 hours)."""
        if seconds < 0 or seconds > MAX_SLOWMODE_SECONDS:
            return await ctx.send(
                f"❌ Slowmode must be between 0 and {MAX_SLOWMODE_SECONDS} seconds (6 hours)"
            )
        await ctx.channel.edit(slowmode_delay=seconds)
        await ctx.send("✅ Slowmode disabled" if seconds == 0 else f"✅ Slowmode set to {seconds} seconds")

    @commands.command()
    @commands.guild_only()
    @with_perms(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def lock(self, ctx, channel: Optional[discord.TextChannel] = None):
        """Lock a channel for @everyone."""
        channel = channel or ctx.channel
        await channel.set_permissions(
            ctx.guild.default_role,
            send_messages=False,
            reason=f"Locked by {ctx.author}",
        )
        await ctx.send(f"{channel.mention} has been locked")

    @commands.command()
    @commands.guild_only()
    @with_perms(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def unlock(self, ctx, channel: Optional[discord.TextChannel] = None):
        """Unlock a channel (clears the @everyone send override)."""
        channel = channel or ctx.channel
        await channel.set_permissions(
            ctx.guild.default_role,
            send_messages=None,
            reason=f"Unlocked by {ctx.author}",
        )
        await ctx.send(f"🔓 {channel.mention} has been unlocked")

    @commands.command()
    @commands.guild_only()
    @commands.check(is_whitelisted)
    @commands.bot_has_permissions(manage_channels=True)
    async def nuke(self, ctx):
        """Delete and recreate the channel. Requires confirmation."""
        view = ConfirmView(ctx.author.id)
        prompt = await ctx.send(
            f"⚠️ This will delete and recreate {ctx.channel.mention}, wiping all messages. Continue?",
            view=view,
        )
        await view.wait()

        if view.value is None:
            try:
                await prompt.edit(content="⌛ Nuke cancelled (timed out).", view=None)
            except discord.HTTPException:
                pass
            return
        if not view.value:
            try:
                await prompt.edit(content="❌ Nuke cancelled.", view=None)
            except discord.HTTPException:
                pass
            return

        position = ctx.channel.position
        try:
            new_channel = await ctx.channel.clone(reason=f"Nuked by {ctx.author}")
            await ctx.channel.delete(reason=f"Nuked by {ctx.author}")
            await new_channel.edit(position=position)
        except discord.Forbidden:
            return await ctx.send("❌ I don't have permission to manage this channel.")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Failed to nuke channel: {e}")

        embed = discord.Embed(
            title="💥 Channel Nuked!",
            description=f"This channel was nuked by {ctx.author.mention}",
            color=discord.Color.red(),
        )
        await new_channel.send(embed=embed)

    @commands.command(aliases=["w"])
    @commands.guild_only()
    @with_perms(manage_roles=True)
    async def warn(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        """Warn a member (sends them a DM if possible)."""
        if err := self._hierarchy_error(ctx, member):
            return await ctx.send(f"❌ {err}")

        embed = discord.Embed(
            title="⚠️ Member Warned",
            description=f"{member.mention} has been warned",
            color=discord.Color.yellow(),
        )
        embed.add_field(name="Reason", value=reason)
        embed.set_footer(text=f"Warned by {ctx.author}")
        await ctx.send(embed=embed)

        try:
            await member.send(f"You have been warned in **{ctx.guild.name}** for: {reason}")
        except (discord.Forbidden, discord.HTTPException):
            pass

async def setup(bot):
    await bot.add_cog(Moderation(bot))
