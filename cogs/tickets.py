"""Ticket system — multi-panel.

Each guild can have multiple named panels (verify / purchase / support / etc.).
Each panel has:
  - an intro embed sent inside opened tickets (a saved embed by name)
  - optional follow-up embeds (e.g. "SERVER LINKS REQUIRED")
  - its own category, staff role, transcript channel
  - its own set of in-ticket action buttons (close, give_access, reject, role)

The slash-action commands `/add /claim /unclaim /close /transcript` work on
ANY ticket regardless of which panel created it.

Backwards compat: the legacy `.ticketsetup`, `.panel`, `.settranscript` still
exist and operate on a panel named `default`.
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

CREATE TABLE IF NOT EXISTS ticket_panels (
    guild_id              BIGINT NOT NULL,
    name                  TEXT NOT NULL,
    intro_embed_name      TEXT,
    extra_embed_names     TEXT,           -- comma-separated list of saved-embed names
    category_id           BIGINT,
    staff_role_id         BIGINT,
    transcript_channel_id BIGINT,
    open_label            TEXT NOT NULL DEFAULT 'Open Ticket',
    open_emoji            TEXT,
    open_style            TEXT NOT NULL DEFAULT 'blurple',
    PRIMARY KEY (guild_id, name)
);

CREATE TABLE IF NOT EXISTS ticket_panel_buttons (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    panel_name  TEXT NOT NULL,
    position    INTEGER NOT NULL,
    action      TEXT NOT NULL,
    label       TEXT NOT NULL,
    style       TEXT NOT NULL DEFAULT 'red',
    emoji       TEXT,
    target      TEXT
);

CREATE INDEX IF NOT EXISTS ticket_panel_buttons_lookup
    ON ticket_panel_buttons (guild_id, panel_name, position);

CREATE TABLE IF NOT EXISTS open_tickets (
    channel_id BIGINT PRIMARY KEY,
    guild_id   BIGINT NOT NULL,
    opener_id  BIGINT NOT NULL,
    claimer_id BIGINT,
    panel_name TEXT,
    opened_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE open_tickets ADD COLUMN IF NOT EXISTS panel_name TEXT;

CREATE INDEX IF NOT EXISTS open_tickets_guild ON open_tickets (guild_id);
"""

STYLE_MAP = {
    "blurple": discord.ButtonStyle.primary,
    "green":   discord.ButtonStyle.success,
    "grey":    discord.ButtonStyle.secondary,
    "gray":    discord.ButtonStyle.secondary,
    "red":     discord.ButtonStyle.danger,
}

VALID_ACTIONS = ("close", "close_reason", "giveaccess", "reject", "role")
ACTIONS_WITH_TARGET = ("giveaccess", "role")


def _parse_emoji(raw: Optional[str]):
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("<") and raw.endswith(">"):
        try:
            return discord.PartialEmoji.from_str(raw)
        except Exception:
            return None
    return raw


# ---------------- Open buttons (panel posts) ----------------

class LegacyOpenTicketButton(discord.ui.DynamicItem[discord.ui.Button],
                             template=r"sentinel:ticketopen:(?P<guild_id>\d+)"):
    """Backwards-compatible button for the legacy single-panel `.panel` command."""

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
        await cog.open_ticket_for(interaction, panel_name="default")


class OpenPanelTicketButton(discord.ui.DynamicItem[discord.ui.Button],
                            template=r"sentinel:tpanel:(?P<panel>[a-z0-9_-]{1,32})"):
    def __init__(self, panel: str, label: str = "Open Ticket",
                 style: discord.ButtonStyle = discord.ButtonStyle.primary,
                 emoji=None):
        super().__init__(
            discord.ui.Button(
                style=style, label=label, emoji=emoji,
                custom_id=f"sentinel:tpanel:{panel}",
            )
        )
        self.panel = panel

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["panel"], label=item.label or "Open Ticket", style=item.style, emoji=item.emoji)

    async def callback(self, interaction: discord.Interaction):
        cog: "Tickets" = interaction.client.get_cog("Tickets")  # type: ignore
        if cog is None:
            return await interaction.response.send_message("❌ Ticket system unavailable.", ephemeral=True)
        await cog.open_ticket_for(interaction, panel_name=self.panel)


# ---------------- In-ticket action buttons ----------------

class TicketActionButton(discord.ui.DynamicItem[discord.ui.Button],
                         template=r"sentinel:tact:(?P<panel>[a-z0-9_-]{1,32}):(?P<pos>\d+)"):
    def __init__(self, panel: str, pos: int, label: str = "Action",
                 style: discord.ButtonStyle = discord.ButtonStyle.danger,
                 emoji=None):
        super().__init__(
            discord.ui.Button(
                style=style, label=label, emoji=emoji,
                custom_id=f"sentinel:tact:{panel}:{pos}",
            )
        )
        self.panel = panel
        self.pos = pos

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["panel"], int(match["pos"]),
                   label=item.label or "Action", style=item.style, emoji=item.emoji)

    async def callback(self, interaction: discord.Interaction):
        cog: "Tickets" = interaction.client.get_cog("Tickets")  # type: ignore
        if cog is None:
            return
        # Look up the action config.
        row = await cog.bot.db.fetchrow(
            "SELECT * FROM ticket_panel_buttons WHERE guild_id=$1 AND panel_name=$2 AND position=$3",
            interaction.guild_id, self.panel, self.pos,
        )
        if row is None:
            return await interaction.response.send_message("❌ Action no longer configured.", ephemeral=True)
        await cog.run_action(interaction, dict(row))


class _CloseReasonModal(discord.ui.Modal, title="Close Ticket"):
    reason_input = discord.ui.TextInput(label="Reason", required=False, max_length=500)

    def __init__(self, cog: "Tickets"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._close_ticket(
            interaction,
            reason=self.reason_input.value.strip() or None,
        )


# ---------------- Cog ----------------

class Tickets(commands.Cog):
    """🎫 Ticket system"""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)
        self.bot.add_dynamic_items(LegacyOpenTicketButton, OpenPanelTicketButton, TicketActionButton)

    # ---------- helpers ----------

    async def _legacy_config(self, guild_id: int) -> Optional[dict]:
        row = await self.bot.db.fetchrow("SELECT * FROM ticket_config WHERE guild_id=$1", guild_id)
        return dict(row) if row else None

    async def _legacy_upsert(self, guild_id: int, **fields):
        await self.bot.db.execute(
            "INSERT INTO ticket_config (guild_id) VALUES ($1) ON CONFLICT DO NOTHING", guild_id,
        )
        if fields:
            sets = ", ".join(f"{k}=${i+2}" for i, k in enumerate(fields))
            await self.bot.db.execute(
                f"UPDATE ticket_config SET {sets} WHERE guild_id=$1",
                guild_id, *fields.values(),
            )

    async def _panel(self, guild_id: int, name: str) -> Optional[dict]:
        row = await self.bot.db.fetchrow(
            "SELECT * FROM ticket_panels WHERE guild_id=$1 AND name=$2", guild_id, name,
        )
        return dict(row) if row else None

    async def _ensure_panel(self, guild_id: int, name: str):
        await self.bot.db.execute(
            "INSERT INTO ticket_panels (guild_id, name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            guild_id, name,
        )

    async def _panel_or_legacy(self, guild_id: int, name: str) -> Optional[dict]:
        """Return the panel config; fall back to legacy ticket_config for 'default'."""
        p = await self._panel(guild_id, name)
        if p and p.get("category_id"):
            return p
        if name == "default":
            legacy = await self._legacy_config(guild_id)
            if legacy and legacy.get("category_id"):
                return {
                    "guild_id": guild_id,
                    "name": "default",
                    "intro_embed_name": None,
                    "extra_embed_names": None,
                    "category_id": legacy.get("category_id"),
                    "staff_role_id": legacy.get("staff_role_id"),
                    "transcript_channel_id": legacy.get("transcript_channel_id"),
                    "open_label": "Open Ticket",
                    "open_emoji": None,
                    "open_style": "blurple",
                }
        return p

    # ---------- open ticket ----------

    async def open_ticket_for(self, interaction: discord.Interaction, panel_name: str = "default"):
        guild = interaction.guild
        cfg = await self._panel_or_legacy(guild.id, panel_name)
        if cfg is None or cfg.get("category_id") is None:
            return await interaction.response.send_message(
                f"❌ Panel `{panel_name}` isn't configured.", ephemeral=True,
            )

        existing = await self.bot.db.fetchrow(
            "SELECT channel_id FROM open_tickets "
            "WHERE guild_id=$1 AND opener_id=$2 AND COALESCE(panel_name, 'default')=$3",
            guild.id, interaction.user.id, panel_name,
        )
        if existing:
            ch = guild.get_channel(existing["channel_id"])
            if ch:
                return await interaction.response.send_message(
                    f"❌ You already have an open ticket: {ch.mention}", ephemeral=True,
                )
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
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, attach_files=True,
                embed_links=True, read_message_history=True,
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True,
                manage_messages=True, embed_links=True,
            ),
        }
        if cfg.get("staff_role_id"):
            staff_role = guild.get_role(cfg["staff_role_id"])
            if staff_role:
                overwrites[staff_role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, manage_messages=True,
                    attach_files=True, embed_links=True,
                )

        try:
            channel = await category.create_text_channel(
                name=f"{panel_name}-{interaction.user.name}".lower()[:90],
                overwrites=overwrites,
                reason=f"Ticket opened by {interaction.user} (panel: {panel_name})",
            )
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ I lack permission to create channels in that category.", ephemeral=True,
            )
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)

        await self.bot.db.execute(
            "INSERT INTO open_tickets (channel_id, guild_id, opener_id, panel_name) "
            "VALUES ($1, $2, $3, $4)",
            channel.id, guild.id, interaction.user.id, panel_name,
        )

        await interaction.response.send_message(f"✅ Ticket opened: {channel.mention}", ephemeral=True)

        # Post intro embed (configured saved embed, or fallback default).
        from utils import embed_script
        from cogs.embeds import fetch_script as _fetch_script

        intro_embed = None
        intro_view = await self._build_action_view(guild.id, panel_name)
        if cfg.get("intro_embed_name"):
            script = await _fetch_script(self.bot, guild.id, cfg["intro_embed_name"])
            if script:
                rendered = embed_script.render(script, user=interaction.user, guild=guild, channel=channel)
                intro_embed = rendered.embed
                content = rendered.content
            else:
                intro_embed = self._fallback_intro(interaction.user)
                content = interaction.user.mention
        else:
            intro_embed = self._fallback_intro(interaction.user)
            content = interaction.user.mention

        try:
            await channel.send(
                content=content,
                embed=intro_embed,
                view=intro_view or discord.utils.MISSING,
            )
        except discord.HTTPException:
            log.exception("Failed to post intro embed in %s", channel)

        # Follow-up embeds.
        if cfg.get("extra_embed_names"):
            extras = [n.strip() for n in cfg["extra_embed_names"].split(",") if n.strip()]
            for extra_name in extras:
                script = await _fetch_script(self.bot, guild.id, extra_name)
                if script is None:
                    continue
                rendered = embed_script.render(script, user=interaction.user, guild=guild, channel=channel)
                try:
                    await channel.send(content=rendered.content, embed=rendered.embed)
                except discord.HTTPException:
                    pass

    @staticmethod
    def _fallback_intro(user: discord.abc.User) -> discord.Embed:
        embed = discord.Embed(
            title="🎫 New Ticket",
            description=f"Hi {user.mention}, a staff member will be with you shortly.",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Opened by {user}")
        return embed

    async def _build_action_view(self, guild_id: int, panel_name: str) -> Optional[discord.ui.View]:
        rows = await self.bot.db.fetch(
            "SELECT * FROM ticket_panel_buttons WHERE guild_id=$1 AND panel_name=$2 ORDER BY position",
            guild_id, panel_name,
        )
        if not rows:
            return None
        view = discord.ui.View(timeout=None)
        for r in rows[:25]:
            style = STYLE_MAP.get(r["style"], discord.ButtonStyle.danger)
            emoji = _parse_emoji(r.get("emoji"))
            view.add_item(TicketActionButton(panel_name, r["position"], label=r["label"], style=style, emoji=emoji))
        return view

    # ---------- run an in-ticket action ----------

    async def run_action(self, interaction: discord.Interaction, button_row: dict):
        action = button_row["action"]
        target = button_row["target"]

        # Permission gate: require staff_role for sensitive actions, opener allowed for plain close.
        ticket_row = await self.bot.db.fetchrow(
            "SELECT * FROM open_tickets WHERE channel_id=$1", interaction.channel_id,
        )
        if ticket_row is None:
            return await interaction.response.send_message("❌ Not a ticket channel.", ephemeral=True)

        cfg = await self._panel_or_legacy(interaction.guild_id, ticket_row["panel_name"] or "default")
        staff_role_id = cfg.get("staff_role_id") if cfg else None
        is_staff = (
            staff_role_id is not None
            and any(r.id == staff_role_id for r in interaction.user.roles)
        ) or interaction.user.guild_permissions.manage_guild
        is_opener = interaction.user.id == ticket_row["opener_id"]

        if action == "close" and not (is_staff or is_opener):
            return await interaction.response.send_message("❌ Only staff or opener can close.", ephemeral=True)
        if action != "close" and not is_staff:
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)

        if action == "close":
            await self._close_ticket(interaction, reason=None)
        elif action == "close_reason":
            await interaction.response.send_modal(_CloseReasonModal(self))
        elif action == "giveaccess":
            await self._give_access(interaction, ticket_row, target)
        elif action == "reject":
            await self._close_ticket(interaction, reason="Rejected")
        elif action == "role":
            await self._toggle_role(interaction, ticket_row, target)

    async def _give_access(self, interaction: discord.Interaction, ticket_row: dict, role_id_str: Optional[str]):
        try:
            role_id = int(role_id_str)
        except (TypeError, ValueError):
            return await interaction.response.send_message("❌ Action target invalid.", ephemeral=True)
        role = interaction.guild.get_role(role_id)
        if role is None:
            return await interaction.response.send_message("❌ Role missing.", ephemeral=True)
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message("❌ Role above mine.", ephemeral=True)
        opener = interaction.guild.get_member(ticket_row["opener_id"])
        if opener is None:
            return await interaction.response.send_message("❌ Opener no longer in server.", ephemeral=True)
        try:
            await opener.add_roles(role, reason=f"Ticket access granted by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("❌ Can't assign role.", ephemeral=True)
        await interaction.response.send_message(
            f"✅ Granted {role.mention} to {opener.mention}. Closing ticket…",
        )
        await self._close_ticket(interaction, reason=f"Access granted ({role.name})", already_responded=True)

    async def _toggle_role(self, interaction: discord.Interaction, ticket_row: dict, role_id_str: Optional[str]):
        try:
            role_id = int(role_id_str)
        except (TypeError, ValueError):
            return await interaction.response.send_message("❌ Action target invalid.", ephemeral=True)
        role = interaction.guild.get_role(role_id)
        if role is None:
            return await interaction.response.send_message("❌ Role missing.", ephemeral=True)
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message("❌ Role above mine.", ephemeral=True)
        opener = interaction.guild.get_member(ticket_row["opener_id"])
        if opener is None:
            return await interaction.response.send_message("❌ Opener no longer in server.", ephemeral=True)
        try:
            if role in opener.roles:
                await opener.remove_roles(role, reason=f"Ticket button by {interaction.user}")
                msg = f"✅ Removed {role.mention} from {opener.mention}."
            else:
                await opener.add_roles(role, reason=f"Ticket button by {interaction.user}")
                msg = f"✅ Added {role.mention} to {opener.mention}."
        except discord.Forbidden:
            return await interaction.response.send_message("❌ Can't toggle role.", ephemeral=True)
        await interaction.response.send_message(msg)

    async def _close_ticket(self, interaction: discord.Interaction, reason: Optional[str], already_responded: bool = False):
        ticket_row = await self.bot.db.fetchrow(
            "SELECT * FROM open_tickets WHERE channel_id=$1", interaction.channel_id,
        )
        if ticket_row is None:
            if not already_responded:
                await interaction.response.send_message("❌ Not a ticket channel.", ephemeral=True)
            return

        cfg = await self._panel_or_legacy(interaction.guild_id, ticket_row["panel_name"] or "default") or {}
        if not already_responded:
            await interaction.response.send_message(
                f"🔒 Closing{f' — {reason}' if reason else ''}…",
            )

        if cfg.get("transcript_channel_id"):
            transcript_channel = interaction.guild.get_channel(cfg["transcript_channel_id"])
            if transcript_channel is not None:
                file = await self._build_transcript(interaction.channel)
                opener = interaction.guild.get_member(ticket_row["opener_id"])
                claimer = interaction.guild.get_member(ticket_row["claimer_id"]) if ticket_row["claimer_id"] else None
                embed = discord.Embed(title="📄 Ticket Closed", color=discord.Color.dark_gray())
                embed.add_field(name="Channel", value=f"#{interaction.channel.name}", inline=True)
                embed.add_field(name="Panel", value=ticket_row["panel_name"] or "default", inline=True)
                embed.add_field(name="Opener", value=opener.mention if opener else f"<@{ticket_row['opener_id']}>", inline=True)
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

    # ---------- legacy admin commands (prefix-only) ----------

    @commands.command(name="ticketsetup")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def ticketsetup(
        self,
        ctx,
        category: discord.CategoryChannel,
        staff_role: discord.Role,
        transcript_channel: Optional[discord.TextChannel] = None,
    ):
        """Configure the default ticket panel. Usage: ticketsetup <category> <@staff> [#transcripts]"""
        await self._legacy_upsert(
            ctx.guild.id,
            category_id=category.id,
            staff_role_id=staff_role.id,
            transcript_channel_id=transcript_channel.id if transcript_channel else None,
        )
        await ctx.send(
            f"✅ Default tickets configured — category {category.mention}, staff {staff_role.mention}"
            + (f", transcripts in {transcript_channel.mention}." if transcript_channel else "."),
        )

    @commands.command(name="settranscript")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def settranscript(self, ctx, channel: discord.TextChannel):
        """Set the transcript log channel for the default panel."""
        await self._legacy_upsert(ctx.guild.id, transcript_channel_id=channel.id)
        await ctx.send(f"✅ Transcripts will go to {channel.mention}.")

    @commands.command(name="panel")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def legacy_panel(self, ctx, channel: discord.TextChannel):
        """Post the default ticket panel button in a channel."""
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        cfg = await self._panel_or_legacy(ctx.guild.id, "default")
        if cfg is None or cfg.get("category_id") is None:
            return await ctx.send(f"❌ Run `{prefix}ticketsetup` first.")

        embed = discord.Embed(
            title="🎫 Open a Ticket",
            description="Click the button below to open a private ticket with staff.",
            color=discord.Color.blurple(),
        )
        view = discord.ui.View(timeout=None)
        view.add_item(LegacyOpenTicketButton(ctx.guild.id))

        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            return await ctx.send(f"❌ I can't send in {channel.mention}.")

        await self._legacy_upsert(
            ctx.guild.id,
            panel_channel_id=channel.id,
            panel_message_id=msg.id,
        )
        await ctx.send(f"✅ Panel posted in {channel.mention}.")

    # ---------- multi-panel commands ----------

    @commands.group(name="ticket", aliases=["t"], invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def ticket(self, ctx):
        """Multi-panel ticket configuration."""
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        await ctx.send(
            f"🎫 **Ticket panels**\n"
            f"`{prefix}ticket panel create <name>`\n"
            f"`{prefix}ticket panel category <name> <#category>`\n"
            f"`{prefix}ticket panel staff <name> <@role>`\n"
            f"`{prefix}ticket panel transcript <name> <#channel>`\n"
            f"`{prefix}ticket panel intro <name> <embed_name>`\n"
            f"`{prefix}ticket panel extra <name> <embed_name1,embed_name2,…>`\n"
            f"`{prefix}ticket panel openbutton <name> <label> [color] [emoji]`\n"
            f"`{prefix}ticket panel button <name> <action> <label> [target] [color] [emoji]`\n"
            f"  · actions: `close`, `close_reason`, `giveaccess <@role>`, `reject`, `role <@role>`\n"
            f"`{prefix}ticket panel removebutton <name> <position>`\n"
            f"`{prefix}ticket panel post <name> <#channel>`\n"
            f"`{prefix}ticket panel list / view <name> / delete <name>`",
        )

    @ticket.group(name="panel", invoke_without_command=True)
    async def ticket_panel(self, ctx):
        """Panel management."""
        await self.ticket(ctx)

    @ticket_panel.command(name="create")
    async def panel_create(self, ctx, name: str):
        """Create a new ticket panel."""
        if not name.replace("_", "").replace("-", "").isalnum() or len(name) > 32:
            return await ctx.send("❌ Name must be 1–32 chars: letters/digits/_/-.")
        try:
            await self.bot.db.execute(
                "INSERT INTO ticket_panels (guild_id, name) VALUES ($1, $2)",
                ctx.guild.id, name,
            )
        except Exception:
            return await ctx.send(f"❌ Panel `{name}` already exists.")
        await ctx.send(f"✅ Panel `{name}` created. Configure it with `ticket panel category/staff/intro/button …`.")

    @ticket_panel.command(name="category")
    async def panel_category(self, ctx, name: str, category: discord.CategoryChannel):
        """Set the category where opened tickets are created."""
        await self._update_panel(ctx, name, category_id=category.id)

    @ticket_panel.command(name="staff")
    async def panel_staff(self, ctx, name: str, role: discord.Role):
        """Set the staff role with access to opened tickets."""
        await self._update_panel(ctx, name, staff_role_id=role.id)

    @ticket_panel.command(name="transcript")
    async def panel_transcript(self, ctx, name: str, channel: discord.TextChannel):
        """Set the transcript log channel."""
        await self._update_panel(ctx, name, transcript_channel_id=channel.id)

    @ticket_panel.command(name="intro")
    async def panel_intro(self, ctx, name: str, embed_name: str):
        """Set the saved embed sent inside opened tickets."""
        from cogs.embeds import fetch_script as _fetch_script
        if await _fetch_script(self.bot, ctx.guild.id, embed_name) is None:
            return await ctx.send(f"❌ No saved embed `{embed_name}`.")
        await self._update_panel(ctx, name, intro_embed_name=embed_name)

    @ticket_panel.command(name="extra")
    async def panel_extra(self, ctx, name: str, *, embed_names: str):
        """Set follow-up saved embeds (comma-separated). Use `none` to clear."""
        if embed_names.lower().strip() == "none":
            await self._update_panel(ctx, name, extra_embed_names=None)
            return
        await self._update_panel(ctx, name, extra_embed_names=embed_names)

    @ticket_panel.command(name="openbutton")
    async def panel_openbutton(self, ctx, name: str, label: str, color: str = "blurple", emoji: Optional[str] = None):
        """Customize the open-panel button (label / color / emoji)."""
        if color not in STYLE_MAP:
            return await ctx.send(f"❌ Color must be one of: {', '.join(STYLE_MAP)}")
        await self._update_panel(ctx, name, open_label=label, open_style=color, open_emoji=emoji)

    @ticket_panel.command(name="button")
    async def panel_addbutton(self, ctx, name: str, action: str, label: str,
                              target: Optional[str] = None, color: str = "red",
                              emoji: Optional[str] = None):
        """Add an in-ticket action button. Actions: close, close_reason, giveaccess, reject, role."""
        action = action.lower()
        if action not in VALID_ACTIONS:
            return await ctx.send(f"❌ Action must be one of: {', '.join(VALID_ACTIONS)}")
        if color not in STYLE_MAP:
            return await ctx.send(f"❌ Color must be one of: {', '.join(STYLE_MAP)}")

        target_val: Optional[str] = None
        if action in ACTIONS_WITH_TARGET:
            if target is None:
                return await ctx.send(f"❌ Action `{action}` requires a role mention/ID as target.")
            try:
                target_val = str(int(target.strip("<@&>")))
            except ValueError:
                return await ctx.send("❌ Target must be a role ID or mention.")
            if ctx.guild.get_role(int(target_val)) is None:
                return await ctx.send("❌ Role not found.")

        if await self._panel(ctx.guild.id, name) is None:
            await self._ensure_panel(ctx.guild.id, name)

        next_pos = await self.bot.db.fetchval(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM ticket_panel_buttons "
            "WHERE guild_id=$1 AND panel_name=$2",
            ctx.guild.id, name,
        )
        await self.bot.db.execute(
            """INSERT INTO ticket_panel_buttons
               (guild_id, panel_name, position, action, label, style, emoji, target)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
            ctx.guild.id, name, next_pos, action, label, color, emoji, target_val,
        )
        await ctx.send(f"✅ Added `{action}` button `{label}` to panel `{name}`.")

    @ticket_panel.command(name="removebutton")
    async def panel_removebutton(self, ctx, name: str, position: int):
        """Remove an in-ticket button by position (1-based)."""
        result = await self.bot.db.execute(
            "DELETE FROM ticket_panel_buttons WHERE guild_id=$1 AND panel_name=$2 AND position=$3",
            ctx.guild.id, name, position - 1,
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        await ctx.send(f"✅ Removed {n} button(s).")

    @ticket_panel.command(name="post")
    async def panel_post(self, ctx, name: str, channel: discord.TextChannel):
        """Post a panel button to a channel so users can open this kind of ticket."""
        cfg = await self._panel(ctx.guild.id, name)
        if cfg is None:
            return await ctx.send(f"❌ No panel `{name}`. Create with `ticket panel create {name}`.")
        if cfg.get("category_id") is None:
            return await ctx.send(f"❌ Panel `{name}` has no category set.")

        intro = discord.Embed(
            title=f"🎫 {name.title()} Ticket",
            description=f"Click the button below to open a {name} ticket.",
            color=discord.Color.blurple(),
        )
        view = discord.ui.View(timeout=None)
        view.add_item(OpenPanelTicketButton(
            name,
            label=cfg["open_label"],
            style=STYLE_MAP.get(cfg["open_style"], discord.ButtonStyle.primary),
            emoji=_parse_emoji(cfg.get("open_emoji")),
        ))
        try:
            await channel.send(embed=intro, view=view)
        except discord.Forbidden:
            return await ctx.send(f"❌ I can't send in {channel.mention}.")
        await ctx.send(f"✅ Posted `{name}` panel in {channel.mention}.")

    @ticket_panel.command(name="list")
    async def panel_list(self, ctx):
        """List all configured panels."""
        rows = await self.bot.db.fetch(
            "SELECT name, category_id, staff_role_id FROM ticket_panels WHERE guild_id=$1 ORDER BY name",
            ctx.guild.id,
        )
        if not rows:
            return await ctx.send("ℹ️ No panels.")
        lines = []
        for r in rows:
            cat = ctx.guild.get_channel(r["category_id"]) if r["category_id"] else None
            staff = ctx.guild.get_role(r["staff_role_id"]) if r["staff_role_id"] else None
            lines.append(f"`{r['name']}` — {cat.mention if cat else '_no cat_'} · {staff.mention if staff else '_no staff_'}")
        await ctx.send("\n".join(lines))

    @ticket_panel.command(name="view")
    async def panel_view(self, ctx, name: str):
        """Show full config for a panel."""
        cfg = await self._panel(ctx.guild.id, name)
        if cfg is None:
            return await ctx.send(f"❌ No panel `{name}`.")
        cat = ctx.guild.get_channel(cfg["category_id"]) if cfg["category_id"] else None
        staff = ctx.guild.get_role(cfg["staff_role_id"]) if cfg["staff_role_id"] else None
        tch = ctx.guild.get_channel(cfg["transcript_channel_id"]) if cfg["transcript_channel_id"] else None
        embed = discord.Embed(title=f"🎫 Panel: {name}", color=discord.Color.blurple())
        embed.add_field(name="Category", value=cat.mention if cat else "—", inline=True)
        embed.add_field(name="Staff", value=staff.mention if staff else "—", inline=True)
        embed.add_field(name="Transcripts", value=tch.mention if tch else "—", inline=True)
        embed.add_field(name="Intro embed", value=cfg["intro_embed_name"] or "—", inline=True)
        embed.add_field(name="Extras", value=cfg["extra_embed_names"] or "—", inline=True)
        embed.add_field(name="Open button", value=f"`{cfg['open_label']}` · {cfg['open_style']}", inline=True)
        btns = await self.bot.db.fetch(
            "SELECT position, action, label, style, target FROM ticket_panel_buttons "
            "WHERE guild_id=$1 AND panel_name=$2 ORDER BY position",
            ctx.guild.id, name,
        )
        if btns:
            lines = [
                f"`{b['position']+1}` {b['action']} · `{b['label']}` ({b['style']})"
                + (f" → <@&{b['target']}>" if b["target"] else "")
                for b in btns
            ]
            embed.add_field(name="In-ticket buttons", value="\n".join(lines), inline=False)
        await ctx.send(embed=embed)

    @ticket_panel.command(name="delete")
    async def panel_delete(self, ctx, name: str):
        """Delete a panel + its buttons."""
        await self.bot.db.execute(
            "DELETE FROM ticket_panel_buttons WHERE guild_id=$1 AND panel_name=$2",
            ctx.guild.id, name,
        )
        result = await self.bot.db.execute(
            "DELETE FROM ticket_panels WHERE guild_id=$1 AND name=$2", ctx.guild.id, name,
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        if n == 0:
            return await ctx.send(f"❌ No panel `{name}`.")
        await ctx.send(f"✅ Deleted panel `{name}`.")

    async def _update_panel(self, ctx, name: str, **fields):
        await self._ensure_panel(ctx.guild.id, name)
        sets = ", ".join(f"{k}=${i+3}" for i, k in enumerate(fields))
        await self.bot.db.execute(
            f"UPDATE ticket_panels SET {sets} WHERE guild_id=$1 AND name=$2",
            ctx.guild.id, name, *fields.values(),
        )
        await ctx.send(f"✅ Updated panel `{name}`.")

    # ---------- slash actions on existing tickets ----------

    async def _ensure_in_ticket(self, interaction: discord.Interaction) -> Optional[dict]:
        row = await self.bot.db.fetchrow(
            "SELECT * FROM open_tickets WHERE channel_id=$1", interaction.channel_id,
        )
        if row is None:
            await interaction.response.send_message("❌ This isn't a ticket channel.", ephemeral=True)
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
        if not await self._ensure_in_ticket(interaction):
            return
        await self._close_ticket(interaction, reason=reason)

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
