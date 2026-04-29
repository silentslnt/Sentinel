"""Server event logging.

Routes Discord gateway events to channels by event category. Categories:
  messages, members, roles, channels, invites, emojis, voice
"""
from __future__ import annotations

import logging as pylog
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

log = pylog.getLogger("sentinel.logging")

EVENT_CATEGORIES = ("messages", "members", "roles", "channels", "invites", "emojis", "voice")
EVENT_CHOICES = [app_commands.Choice(name=e, value=e) for e in EVENT_CATEGORIES]

SCHEMA = """
CREATE TABLE IF NOT EXISTS log_routes (
    guild_id   BIGINT NOT NULL,
    event      TEXT NOT NULL,
    channel_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, event)
);

CREATE TABLE IF NOT EXISTS log_ignores (
    guild_id BIGINT NOT NULL,
    target_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, target_id)
);
"""


class Logging(commands.Cog):
    """📝 Event logging"""

    def __init__(self, bot):
        self.bot = bot
        self._routes: dict[int, dict[str, int]] = {}
        self._ignored: dict[int, set[int]] = {}

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)
        await self._refresh()

    async def _refresh(self):
        rows = await self.bot.db.fetch("SELECT * FROM log_routes")
        self._routes = {}
        for r in rows:
            self._routes.setdefault(r["guild_id"], {})[r["event"]] = r["channel_id"]
        ig = await self.bot.db.fetch("SELECT * FROM log_ignores")
        self._ignored = {}
        for r in ig:
            self._ignored.setdefault(r["guild_id"], set()).add(r["target_id"])

    def _channel_for(self, guild: discord.Guild, event: str) -> Optional[discord.TextChannel]:
        cid = self._routes.get(guild.id, {}).get(event)
        if cid is None:
            return None
        ch = guild.get_channel(cid)
        return ch if isinstance(ch, discord.TextChannel) else None

    def _is_ignored(self, guild_id: int, *target_ids: int) -> bool:
        ignored = self._ignored.get(guild_id, set())
        return any(t in ignored for t in target_ids if t)

    async def _emit(self, guild: discord.Guild, event: str, embed: discord.Embed):
        channel = self._channel_for(guild, event)
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ---------------- listeners ----------------

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        if self._is_ignored(message.guild.id, message.author.id, message.channel.id):
            return
        embed = discord.Embed(
            title="🗑️ Message Deleted",
            description=message.content or "_no text content_",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(name="Author ID", value=str(message.author.id), inline=True)
        await self._emit(message.guild, "messages", embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.guild is None or after.author.bot or before.content == after.content:
            return
        if self._is_ignored(after.guild.id, after.author.id, after.channel.id):
            return
        embed = discord.Embed(
            title="✏️ Message Edited",
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=str(after.author), icon_url=after.author.display_avatar.url)
        embed.add_field(name="Before", value=(before.content[:1024] or "_empty_"), inline=False)
        embed.add_field(name="After", value=(after.content[:1024] or "_empty_"), inline=False)
        embed.add_field(name="Channel", value=after.channel.mention, inline=True)
        embed.add_field(name="Jump", value=f"[link]({after.jump_url})", inline=True)
        await self._emit(after.guild, "messages", embed)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if self._is_ignored(member.guild.id, member.id):
            return
        embed = discord.Embed(
            title="📥 Member Joined",
            description=f"{member.mention} ({member})",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Account Created", value=f"<t:{int(member.created_at.timestamp())}:R>", inline=True)
        embed.add_field(name="Member Count", value=str(member.guild.member_count), inline=True)
        await self._emit(member.guild, "members", embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if self._is_ignored(member.guild.id, member.id):
            return
        embed = discord.Embed(
            title="📤 Member Left",
            description=f"{member.mention} ({member})",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        if member.joined_at:
            embed.add_field(name="Joined", value=f"<t:{int(member.joined_at.timestamp())}:R>", inline=True)
        embed.add_field(name="Member Count", value=str(member.guild.member_count), inline=True)
        await self._emit(member.guild, "members", embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if self._is_ignored(after.guild.id, after.id):
            return
        if before.roles != after.roles:
            added = [r for r in after.roles if r not in before.roles]
            removed = [r for r in before.roles if r not in after.roles]
            if added or removed:
                embed = discord.Embed(
                    title="🧩 Member Roles Changed",
                    description=f"{after.mention} ({after})",
                    color=discord.Color.blurple(),
                    timestamp=discord.utils.utcnow(),
                )
                if added:
                    embed.add_field(name="Added", value=", ".join(r.mention for r in added), inline=False)
                if removed:
                    embed.add_field(name="Removed", value=", ".join(r.mention for r in removed), inline=False)
                await self._emit(after.guild, "roles", embed)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        embed = discord.Embed(title="➕ Role Created", description=f"{role.mention} `{role.name}`",
                              color=discord.Color.green(), timestamp=discord.utils.utcnow())
        await self._emit(role.guild, "roles", embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        embed = discord.Embed(title="➖ Role Deleted", description=f"`{role.name}` (`{role.id}`)",
                              color=discord.Color.red(), timestamp=discord.utils.utcnow())
        await self._emit(role.guild, "roles", embed)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        embed = discord.Embed(title="➕ Channel Created", description=f"{channel.mention} `{channel.name}`",
                              color=discord.Color.green(), timestamp=discord.utils.utcnow())
        await self._emit(channel.guild, "channels", embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        embed = discord.Embed(title="➖ Channel Deleted", description=f"`{channel.name}` (`{channel.id}`)",
                              color=discord.Color.red(), timestamp=discord.utils.utcnow())
        await self._emit(channel.guild, "channels", embed)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        if invite.guild is None:
            return
        embed = discord.Embed(title="🔗 Invite Created", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Code", value=f"`{invite.code}`", inline=True)
        embed.add_field(name="Channel", value=invite.channel.mention if invite.channel else "—", inline=True)
        if invite.inviter:
            embed.add_field(name="Inviter", value=invite.inviter.mention, inline=True)
        await self._emit(invite.guild, "invites", embed)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        if invite.guild is None:
            return
        embed = discord.Embed(title="🔗 Invite Deleted", description=f"`{invite.code}`",
                              color=discord.Color.red(), timestamp=discord.utils.utcnow())
        await self._emit(invite.guild, "invites", embed)

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before, after):
        added = [e for e in after if e not in before]
        removed = [e for e in before if e not in after]
        if not (added or removed):
            return
        embed = discord.Embed(title="😀 Emojis Updated", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        if added:
            embed.add_field(name="Added", value=" ".join(str(e) for e in added)[:1024], inline=False)
        if removed:
            embed.add_field(name="Removed", value=", ".join(f"`:{e.name}:`" for e in removed)[:1024], inline=False)
        await self._emit(guild, "emojis", embed)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot or before.channel == after.channel:
            return
        if self._is_ignored(member.guild.id, member.id):
            return
        if before.channel is None and after.channel is not None:
            title, color = "🔊 Joined Voice", discord.Color.green()
            desc = f"{member.mention} → {after.channel.mention}"
        elif before.channel is not None and after.channel is None:
            title, color = "🔇 Left Voice", discord.Color.orange()
            desc = f"{member.mention} ← {before.channel.mention}"
        else:
            title, color = "🔁 Moved Voice", discord.Color.blurple()
            desc = f"{member.mention}: {before.channel.mention} → {after.channel.mention}"
        embed = discord.Embed(title=title, description=desc, color=color, timestamp=discord.utils.utcnow())
        await self._emit(member.guild, "voice", embed)

    # ---------------- commands ----------------

    log_group = app_commands.Group(
        name="log",
        description="Configure event logging",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @log_group.command(name="add", description="Route an event category to a channel")
    @app_commands.choices(event=EVENT_CHOICES)
    async def log_add(self, interaction: discord.Interaction, event: app_commands.Choice[str], channel: discord.TextChannel):
        await self.bot.db.execute(
            """INSERT INTO log_routes (guild_id, event, channel_id) VALUES ($1, $2, $3)
               ON CONFLICT (guild_id, event) DO UPDATE SET channel_id = EXCLUDED.channel_id""",
            interaction.guild_id, event.value, channel.id,
        )
        await self._refresh()
        await interaction.response.send_message(
            f"✅ `{event.value}` events will log in {channel.mention}.", ephemeral=True,
        )

    @log_group.command(name="remove", description="Stop logging an event category")
    @app_commands.choices(event=EVENT_CHOICES)
    async def log_remove(self, interaction: discord.Interaction, event: app_commands.Choice[str]):
        await self.bot.db.execute(
            "DELETE FROM log_routes WHERE guild_id=$1 AND event=$2",
            interaction.guild_id, event.value,
        )
        await self._refresh()
        await interaction.response.send_message(f"✅ Removed `{event.value}` route.", ephemeral=True)

    @log_group.command(name="ignore", description="Ignore a member or channel from logging")
    async def log_ignore(self, interaction: discord.Interaction, target: str):
        try:
            target_id = int(target.strip("<@#!&>"))
        except ValueError:
            return await interaction.response.send_message("❌ Provide a user/channel mention or ID.", ephemeral=True)
        await self.bot.db.execute(
            "INSERT INTO log_ignores (guild_id, target_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            interaction.guild_id, target_id,
        )
        await self._refresh()
        await interaction.response.send_message(f"✅ Ignoring `{target_id}`.", ephemeral=True)

    @log_group.command(name="unignore", description="Stop ignoring a member or channel")
    async def log_unignore(self, interaction: discord.Interaction, target: str):
        try:
            target_id = int(target.strip("<@#!&>"))
        except ValueError:
            return await interaction.response.send_message("❌ Provide a user/channel mention or ID.", ephemeral=True)
        await self.bot.db.execute(
            "DELETE FROM log_ignores WHERE guild_id=$1 AND target_id=$2",
            interaction.guild_id, target_id,
        )
        await self._refresh()
        await interaction.response.send_message(f"✅ No longer ignoring `{target_id}`.", ephemeral=True)

    @log_group.command(name="list", description="Show current log routes")
    async def log_list(self, interaction: discord.Interaction):
        routes = self._routes.get(interaction.guild_id, {})
        ignored = self._ignored.get(interaction.guild_id, set())
        if not routes and not ignored:
            return await interaction.response.send_message("ℹ️ No logging configured.", ephemeral=True)
        lines = []
        for ev, cid in routes.items():
            ch = interaction.guild.get_channel(cid)
            lines.append(f"• `{ev}` → {ch.mention if ch else f'<#{cid}>'}")
        if ignored:
            lines.append("\n**Ignored:** " + ", ".join(f"`{i}`" for i in ignored))
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Logging(bot))
