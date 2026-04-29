"""Ticket system.

Concepts:
  - One **panel** message per guild (lives in a chosen channel) with an "Open Ticket"
    button. Clicking it creates a private channel for the user under the configured
    category, with view/send permissions for the user + staff role.
  - Tickets can be claimed/unclaimed/closed/added-to. On close, a transcript (HTML)
    can be exported to the configured transcript channel.

Schema is intentionally minimal — one config row per guild, one row per open ticket.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("sentinel.tickets")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ticket_config (
    guild_id              BIGINT PRIMARY KEY,
    panel_channel_id      BIGINT,
    panel_message_id      BIGINT,
    category_id           BIGINT,
    staff_role_id         BIGINT,
    transcript_channel_id BIGINT
);

CREATE TABLE IF NOT EXISTS open_tickets (
    channel_id BIGINT PRIMARY KEY,
    guild_id   BIGINT NOT NULL,
    opener_id  BIGINT NOT NULL,
    claimer_id BIGINT,
    opened_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS open_tickets_guild ON open_tickets (guild_id);
"""


class OpenTicketButton(discord.ui.DynamicItem[discord.ui.Button],
                       template=r"sentinel:ticketopen:(?P<guild_id>\d+)"):
    def __init__(self, guild_id: int):
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label="Open Ticket",
                emoji="🎫",
                custom_id=f"sentinel:ticketopen:{guild_id}",
            )
        )
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction):
        cog: "Tickets" = interaction.client.get_cog("Tickets")  # type: ignore
        if cog is None:
            return await interaction.response.send_message("❌ Ticket system unavailable.", ephemeral=True)
        await cog.open_ticket_for(interaction)


class Tickets(commands.Cog):
    """🎫 Ticket system"""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)
        self.bot.add_dynamic_items(OpenTicketButton)

    async def _config(self, guild_id: int) -> Optional[dict]:
        row = await self.bot.db.fetchrow("SELECT * FROM ticket_config WHERE guild_id=$1", guild_id)
        return dict(row) if row else None

    async def _upsert_config(self, guild_id: int, **fields):
        await self.bot.db.execute(
            "INSERT INTO ticket_config (guild_id) VALUES ($1) ON CONFLICT DO NOTHING", guild_id,
        )
        if fields:
            sets = ", ".join(f"{k}=${i+2}" for i, k in enumerate(fields))
            await self.bot.db.execute(
                f"UPDATE ticket_config SET {sets} WHERE guild_id=$1",
                guild_id, *fields.values(),
            )

    # ------------------- ticket lifecycle -------------------

    async def open_ticket_for(self, interaction: discord.Interaction):
        guild = interaction.guild
        cfg = await self._config(guild.id)
        if cfg is None or cfg.get("category_id") is None:
            return await interaction.response.send_message(
                "❌ Ticket system isn't fully configured. Ask a staff member to run `/ticketsetup`.",
                ephemeral=True,
            )

        # One open ticket per user per guild (cheap to enforce).
        existing = await self.bot.db.fetchrow(
            "SELECT channel_id FROM open_tickets WHERE guild_id=$1 AND opener_id=$2",
            guild.id, interaction.user.id,
        )
        if existing:
            ch = guild.get_channel(existing["channel_id"])
            if ch:
                return await interaction.response.send_message(
                    f"❌ You already have an open ticket: {ch.mention}", ephemeral=True,
                )
            else:
                # Stale row; clean up.
                await self.bot.db.execute(
                    "DELETE FROM open_tickets WHERE channel_id=$1", existing["channel_id"],
                )

        category = guild.get_channel(cfg["category_id"])
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message(
                "❌ Configured ticket category is missing.", ephemeral=True,
            )

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True, embed_links=True),
        }
        if cfg.get("staff_role_id"):
            staff_role = guild.get_role(cfg["staff_role_id"])
            if staff_role:
                overwrites[staff_role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, manage_messages=True, attach_files=True, embed_links=True,
                )

        try:
            channel = await category.create_text_channel(
                name=f"ticket-{interaction.user.name}".lower()[:90],
                overwrites=overwrites,
                reason=f"Ticket opened by {interaction.user}",
            )
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I lack permission to create channels in that category.", ephemeral=True)
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)

        await self.bot.db.execute(
            "INSERT INTO open_tickets (channel_id, guild_id, opener_id) VALUES ($1, $2, $3)",
            channel.id, guild.id, interaction.user.id,
        )

        await interaction.response.send_message(f"✅ Ticket opened: {channel.mention}", ephemeral=True)

        embed = discord.Embed(
            title="🎫 New Ticket",
            description=f"Hi {interaction.user.mention}, a staff member will be with you shortly.\n\n"
                        f"Use `/close` to close this ticket.",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Opened by {interaction.user}")
        await channel.send(content=interaction.user.mention, embed=embed)

    # ------------------- commands -------------------

    @app_commands.command(name="ticketsetup", description="Configure the ticket system")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        category="Category where new ticket channels are created",
        staff_role="Role granted access to all tickets",
        transcript_channel="Where ticket transcripts are posted on close (optional)",
    )
    async def ticketsetup(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        staff_role: discord.Role,
        transcript_channel: Optional[discord.TextChannel] = None,
    ):
        await self._upsert_config(
            interaction.guild_id,
            category_id=category.id,
            staff_role_id=staff_role.id,
            transcript_channel_id=transcript_channel.id if transcript_channel else None,
        )
        await interaction.response.send_message(
            f"✅ Tickets configured — category {category.mention}, staff {staff_role.mention}"
            + (f", transcripts in {transcript_channel.mention}." if transcript_channel else "."),
            ephemeral=True,
        )

    @app_commands.command(name="panel", description="Post the ticket panel in a channel")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def panel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        cfg = await self._config(interaction.guild_id)
        if cfg is None or cfg.get("category_id") is None:
            return await interaction.response.send_message(
                "❌ Run `/ticketsetup` first.", ephemeral=True,
            )

        embed = discord.Embed(
            title="🎫 Open a Ticket",
            description="Click the button below to open a private ticket with staff.",
            color=discord.Color.blurple(),
        )
        view = discord.ui.View(timeout=None)
        view.add_item(OpenTicketButton(interaction.guild_id))

        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            return await interaction.response.send_message(f"❌ I can't send in {channel.mention}.", ephemeral=True)

        await self._upsert_config(
            interaction.guild_id,
            panel_channel_id=channel.id,
            panel_message_id=msg.id,
        )
        await interaction.response.send_message(f"✅ Panel posted in {channel.mention}.", ephemeral=True)

    @app_commands.command(name="settranscript", description="Set the transcript log channel")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def settranscript(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self._upsert_config(interaction.guild_id, transcript_channel_id=channel.id)
        await interaction.response.send_message(f"✅ Transcripts will go to {channel.mention}.", ephemeral=True)

    async def _ensure_in_ticket(self, interaction: discord.Interaction) -> Optional[dict]:
        row = await self.bot.db.fetchrow(
            "SELECT * FROM open_tickets WHERE channel_id=$1", interaction.channel_id,
        )
        if row is None:
            await interaction.response.send_message(
                "❌ This isn't a ticket channel.", ephemeral=True,
            )
            return None
        return dict(row)

    @app_commands.command(name="add", description="Add a user to this ticket")
    @app_commands.guild_only()
    async def add(self, interaction: discord.Interaction, user: discord.Member):
        if not await self._ensure_in_ticket(interaction):
            return
        try:
            await interaction.channel.set_permissions(
                user, view_channel=True, send_messages=True, read_message_history=True,
                reason=f"Added by {interaction.user}",
            )
        except discord.Forbidden:
            return await interaction.response.send_message("❌ Missing permission.", ephemeral=True)
        await interaction.response.send_message(f"✅ Added {user.mention}.")

    @app_commands.command(name="claim", description="Claim this ticket")
    @app_commands.guild_only()
    async def claim(self, interaction: discord.Interaction):
        row = await self._ensure_in_ticket(interaction)
        if row is None:
            return
        if row["claimer_id"] is not None:
            claimer = interaction.guild.get_member(row["claimer_id"])
            return await interaction.response.send_message(
                f"❌ Already claimed by {claimer.mention if claimer else 'someone'}.",
                ephemeral=True,
            )
        await self.bot.db.execute(
            "UPDATE open_tickets SET claimer_id=$2 WHERE channel_id=$1",
            interaction.channel_id, interaction.user.id,
        )
        await interaction.response.send_message(f"✅ {interaction.user.mention} claimed this ticket.")

    @app_commands.command(name="unclaim", description="Release your claim on this ticket")
    @app_commands.guild_only()
    async def unclaim(self, interaction: discord.Interaction):
        row = await self._ensure_in_ticket(interaction)
        if row is None:
            return
        if row["claimer_id"] != interaction.user.id and not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("❌ You didn't claim this ticket.", ephemeral=True)
        await self.bot.db.execute(
            "UPDATE open_tickets SET claimer_id=NULL WHERE channel_id=$1",
            interaction.channel_id,
        )
        await interaction.response.send_message("✅ Ticket unclaimed.")

    @app_commands.command(name="close", description="Close this ticket")
    @app_commands.guild_only()
    @app_commands.describe(reason="Optional close reason")
    async def close(self, interaction: discord.Interaction, reason: Optional[str] = None):
        row = await self._ensure_in_ticket(interaction)
        if row is None:
            return
        cfg = await self._config(interaction.guild_id) or {}
        await interaction.response.send_message(
            f"🔒 Closing ticket{f' — {reason}' if reason else ''}…", ephemeral=False,
        )

        # Generate + send transcript before deleting.
        if cfg.get("transcript_channel_id"):
            transcript_channel = interaction.guild.get_channel(cfg["transcript_channel_id"])
            if transcript_channel is not None:
                file = await self._build_transcript(interaction.channel)
                opener = interaction.guild.get_member(row["opener_id"])
                claimer = interaction.guild.get_member(row["claimer_id"]) if row["claimer_id"] else None
                embed = discord.Embed(title="📄 Ticket Closed", color=discord.Color.dark_gray())
                embed.add_field(name="Channel", value=f"#{interaction.channel.name}", inline=True)
                embed.add_field(name="Opener", value=opener.mention if opener else f"<@{row['opener_id']}>", inline=True)
                embed.add_field(name="Claimed by", value=claimer.mention if claimer else "—", inline=True)
                embed.add_field(name="Closed by", value=interaction.user.mention, inline=True)
                if reason:
                    embed.add_field(name="Reason", value=reason, inline=False)
                try:
                    await transcript_channel.send(embed=embed, file=file)
                except (discord.Forbidden, discord.HTTPException):
                    pass

        await self.bot.db.execute(
            "DELETE FROM open_tickets WHERE channel_id=$1", interaction.channel_id,
        )
        try:
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
        except (discord.Forbidden, discord.HTTPException):
            pass

    @app_commands.command(name="transcript", description="Generate the transcript for this ticket")
    @app_commands.guild_only()
    async def transcript(self, interaction: discord.Interaction):
        if not await self._ensure_in_ticket(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        file = await self._build_transcript(interaction.channel)
        await interaction.followup.send(file=file, ephemeral=True)

    async def _build_transcript(self, channel: discord.TextChannel) -> discord.File:
        lines = [
            "<!doctype html><meta charset='utf-8'>",
            f"<title>Transcript: #{channel.name}</title>",
            "<style>body{font-family:sans-serif;max-width:800px;margin:2em auto;padding:0 1em}"
            ".m{margin:.5em 0;padding:.5em;border-left:3px solid #5865F2;background:#f5f5f7}"
            ".a{font-weight:600}.t{color:#666;font-size:.85em;margin-left:.5em}</style>",
            f"<h1>#{channel.name}</h1>",
        ]
        async for msg in channel.history(limit=None, oldest_first=True):
            ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            content = discord.utils.escape_markdown(msg.content or "")
            content = content.replace("\n", "<br>")
            attach = ""
            if msg.attachments:
                attach = "<br>" + "<br>".join(
                    f"<a href='{a.url}'>{discord.utils.escape_markdown(a.filename)}</a>"
                    for a in msg.attachments
                )
            lines.append(
                f"<div class='m'><span class='a'>{discord.utils.escape_markdown(str(msg.author))}</span>"
                f"<span class='t'>{ts}</span><br>{content}{attach}</div>"
            )
        html = "\n".join(lines).encode("utf-8")
        return discord.File(io.BytesIO(html), filename=f"transcript-{channel.name}.html")


async def setup(bot):
    await bot.add_cog(Tickets(bot))
