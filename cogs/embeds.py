"""Embed builder + saved-embeds library + persistent button actions.

Three button types are supported on saved embeds:
  - link          → standard URL button
  - role          → toggle the configured role on/off (ephemeral confirmation)
  - open          → send another saved embed ephemerally to the clicker

Buttons survive bot restarts via DynamicItem custom_id matching.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import discord
from discord.ext import commands

from utils import embed_script
from utils.embed_builder import EmbedBuilderView

log = logging.getLogger("sentinel.embeds")

SCHEMA = """
CREATE TABLE IF NOT EXISTS saved_embeds (
    guild_id BIGINT NOT NULL,
    name     TEXT   NOT NULL,
    script   TEXT   NOT NULL,
    PRIMARY KEY (guild_id, name)
);

CREATE TABLE IF NOT EXISTS embed_buttons (
    id         BIGSERIAL PRIMARY KEY,
    guild_id   BIGINT NOT NULL,
    embed_name TEXT NOT NULL,
    position   INTEGER NOT NULL,
    style      TEXT NOT NULL,  -- 'link' | 'role' | 'open' | 'plain' | 'form' | 'ticket' | 'verify'
    color      TEXT NOT NULL DEFAULT 'blurple',
    label      TEXT NOT NULL,
    target     TEXT,
    emoji      TEXT
);

ALTER TABLE embed_buttons ADD COLUMN IF NOT EXISTS emoji TEXT;

CREATE INDEX IF NOT EXISTS embed_buttons_lookup
    ON embed_buttons (guild_id, embed_name, position);
"""

NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

COLOR_STYLES = {
    "blurple": discord.ButtonStyle.primary,
    "green":   discord.ButtonStyle.success,
    "grey":    discord.ButtonStyle.secondary,
    "gray":    discord.ButtonStyle.secondary,
    "red":     discord.ButtonStyle.danger,
}


# ---------------- Persistent dynamic buttons ----------------

class RoleToggleButton(discord.ui.DynamicItem[discord.ui.Button],
                       template=r"sentinel:roletoggle:(?P<role_id>\d+)"):
    def __init__(self, role_id: int, label: str = "Role",
                 style: discord.ButtonStyle = discord.ButtonStyle.primary,
                 emoji=None):
        super().__init__(
            discord.ui.Button(
                style=style,
                label=label,
                emoji=emoji,
                custom_id=f"sentinel:roletoggle:{role_id}",
            )
        )
        self.role_id = role_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(int(match["role_id"]), label=item.label or "Role", style=item.style, emoji=item.emoji)

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        role = guild.get_role(self.role_id)
        if role is None:
            return await interaction.response.send_message("❌ That role no longer exists.", ephemeral=True)
        if role >= guild.me.top_role:
            return await interaction.response.send_message(
                "❌ I can't manage that role (it's above mine).", ephemeral=True,
            )
        member = interaction.user
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Embed button")
                await interaction.response.send_message(f"✅ Removed {role.mention}.", ephemeral=True)
            else:
                await member.add_roles(role, reason="Embed button")
                await interaction.response.send_message(f"✅ Added {role.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I'm missing permission to do that.", ephemeral=True)


class OpenEmbedButton(discord.ui.DynamicItem[discord.ui.Button],
                      template=r"sentinel:embedopen:(?P<name>[a-z0-9_-]{1,32})"):
    def __init__(self, name: str, label: str = "Open",
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary,
                 emoji=None):
        super().__init__(
            discord.ui.Button(
                style=style,
                label=label,
                emoji=emoji,
                custom_id=f"sentinel:embedopen:{name}",
            )
        )
        self.name = name

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(match["name"], label=item.label or "Open", style=item.style, emoji=item.emoji)


class OpenFormButton(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"sentinel:formopen:(?P<name>[a-z0-9_-]{1,32})"):
    def __init__(self, name: str, label: str = "Open Form",
                 style: discord.ButtonStyle = discord.ButtonStyle.primary,
                 emoji=None):
        super().__init__(
            discord.ui.Button(
                style=style, label=label, emoji=emoji,
                custom_id=f"sentinel:formopen:{name}",
            )
        )
        self.name = name

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["name"], label=item.label or "Open Form", style=item.style, emoji=item.emoji)

    async def callback(self, interaction: discord.Interaction):
        from cogs.forms import render_form_for
        await render_form_for(interaction.client, interaction, self.name)


class OpenTicketPanelButton(discord.ui.DynamicItem[discord.ui.Button],
                            template=r"sentinel:tpanelopen:(?P<name>[a-z0-9_-]{1,32})"):
    """Used as an embed-button to open a specific named ticket panel."""

    def __init__(self, name: str, label: str = "Open Ticket",
                 style: discord.ButtonStyle = discord.ButtonStyle.primary,
                 emoji=None):
        super().__init__(
            discord.ui.Button(
                style=style, label=label, emoji=emoji,
                custom_id=f"sentinel:tpanelopen:{name}",
            )
        )
        self.name = name

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["name"], label=item.label or "Open Ticket", style=item.style, emoji=item.emoji)

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Tickets")
        if cog is None:
            return await interaction.response.send_message("❌ Tickets unavailable.", ephemeral=True)
        await cog.open_ticket_for(interaction, panel_name=self.name)


class StartVerifyEmbedButton(discord.ui.DynamicItem[discord.ui.Button],
                             template=r"sentinel:emverify:(?P<gid>\d+)"):
    """Embed-attachable verify-start button (forwards to the Verify cog)."""

    def __init__(self, gid: int, label: str = "Verify",
                 style: discord.ButtonStyle = discord.ButtonStyle.success,
                 emoji=None):
        super().__init__(
            discord.ui.Button(
                style=style, label=label, emoji=emoji,
                custom_id=f"sentinel:emverify:{gid}",
            )
        )
        self.gid = gid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["gid"]), label=item.label or "Verify", style=item.style, emoji=item.emoji)

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Verify")
        if cog is None:
            return await interaction.response.send_message("❌ Verify unavailable.", ephemeral=True)
        await cog.start_verification(interaction)

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        row = await bot.db.fetchrow(
            "SELECT script FROM saved_embeds WHERE guild_id=$1 AND name=$2",
            interaction.guild_id, self.name,
        )
        if row is None:
            return await interaction.response.send_message(
                f"❌ Embed `{self.name}` no longer exists.", ephemeral=True,
            )
        rendered = embed_script.render(
            row["script"],
            user=interaction.user,
            guild=interaction.guild,
            channel=interaction.channel,
        )
        view = await build_view(bot, interaction.guild_id, self.name)
        await interaction.response.send_message(
            content=rendered.content,
            embed=rendered.embed,
            view=view or discord.utils.MISSING,
            ephemeral=True,
        )


# ---------------- Helpers ----------------

def _parse_emoji(raw: Optional[str]):
    """Convert a stored emoji string (unicode or `<:name:id>`) to something Discord accepts."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    # Custom emoji: <:name:id> or <a:name:id>
    if raw.startswith("<") and raw.endswith(">"):
        try:
            return discord.PartialEmoji.from_str(raw)
        except Exception:
            return None
    # Unicode emoji — Discord accepts the literal string
    return raw


async def build_view(bot, guild_id: int, embed_name: str) -> Optional[discord.ui.View]:
    """Build the persistent view for a saved embed's buttons."""
    rows = await bot.db.fetch(
        "SELECT * FROM embed_buttons WHERE guild_id=$1 AND embed_name=$2 ORDER BY position",
        guild_id, embed_name,
    )
    if not rows:
        return None
    view = discord.ui.View(timeout=None)
    for r in rows[:25]:
        style_color = COLOR_STYLES.get(r["color"], discord.ButtonStyle.secondary)
        emoji = _parse_emoji(r.get("emoji"))
        if r["style"] == "link":
            view.add_item(discord.ui.Button(
                style=discord.ButtonStyle.link, label=r["label"], url=r["target"], emoji=emoji,
            ))
        elif r["style"] == "role":
            try:
                view.add_item(RoleToggleButton(int(r["target"]), label=r["label"], style=style_color, emoji=emoji))
            except (TypeError, ValueError):
                continue
        elif r["style"] == "open":
            view.add_item(OpenEmbedButton(r["target"], label=r["label"], style=style_color, emoji=emoji))
        elif r["style"] == "form":
            view.add_item(OpenFormButton(r["target"], label=r["label"], style=style_color, emoji=emoji))
        elif r["style"] == "ticket":
            view.add_item(OpenTicketPanelButton(r["target"], label=r["label"], style=style_color, emoji=emoji))
        elif r["style"] == "verify":
            try:
                view.add_item(StartVerifyEmbedButton(int(r["target"]), label=r["label"], style=style_color, emoji=emoji))
            except (TypeError, ValueError):
                continue
        else:  # plain (decorative)
            view.add_item(discord.ui.Button(
                style=style_color, label=r["label"], disabled=True, emoji=emoji,
                custom_id=f"sentinel:plain:{guild_id}:{embed_name}:{r['position']}",
            ))
    return view


async def fetch_script(bot, guild_id: int, name: str) -> Optional[str]:
    return await bot.db.fetchval(
        "SELECT script FROM saved_embeds WHERE guild_id=$1 AND name=$2",
        guild_id, name,
    )


# ---------------- Cog ----------------

class Embeds(commands.Cog):
    """🧱 Embed builder & library"""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)
        # Register dynamic item handlers globally so saved embeds keep working after restarts.
        self.bot.add_dynamic_items(
            RoleToggleButton, OpenEmbedButton,
            OpenFormButton, OpenTicketPanelButton, StartVerifyEmbedButton,
        )

    @commands.group(name="embed", aliases=["em"], invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def embed(self, ctx):
        """Embed builder & library."""
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        embed = discord.Embed(
            title="🧱 Embed Builder",
            description=(
                f"**Step 1 — open the guided builder:**\n"
                f"`{prefix}embed create <name>`\n"
                f"This sends a message with **12 buttons**: Title · Description · Color · Author · "
                f"Footer · Image · Thumbnail · Add Field · Clear Fields · 👁 Preview · ✅ Save · 🗑 Discard.\n"
                f"Each button opens a small modal for just that field. The preview embed updates in "
                f"real time. Click **✅ Save** when done.\n\n"
                f"**Step 2 — attach buttons to your saved embed:**\n"
                f"`{prefix}embed button addlink <name> <label> <url> [emoji]`\n"
                f"`{prefix}embed button addrole <name> <label> <@role> [emoji]`\n"
                f"`{prefix}embed button addopen <name> <label> <other_embed>` · opens another embed ephemerally\n"
                f"`{prefix}embed button addform <name> <label> <form>` · opens a form\n"
                f"`{prefix}embed button addticket <name> <label> <panel>` · opens a ticket panel\n"
                f"`{prefix}embed button addverify <name> [label]` · starts verification\n"
                f"`{prefix}embed button list <name>` · `{prefix}embed button remove <name> <pos>`\n\n"
                f"**Step 3 — send it:**\n"
                f"`{prefix}embed send <name> <#channel>`\n\n"
                f"**Library:** `list`, `preview`, `edit`, `delete`, `raw`, `save <name> <script>`"
            ),
            color=discord.Color(0xFFFFFF),
        )
        embed.set_footer(text=f"Aliases: {prefix}em · {prefix}embed")
        await ctx.send(embed=embed)

    @embed.command(name="create")
    async def create(self, ctx, name: str):
        """Open the guided embed builder."""
        if not NAME_RE.match(name):
            return await ctx.send("❌ Name must be 1–32 chars: lowercase letters, digits, `_`, `-`.")

        async def on_save(modal_interaction: discord.Interaction, save_name: str, script: str) -> bool:
            await self.bot.db.execute(
                """INSERT INTO saved_embeds (guild_id, name, script) VALUES ($1, $2, $3)
                   ON CONFLICT (guild_id, name) DO UPDATE SET script = EXCLUDED.script""",
                modal_interaction.guild_id, save_name, script,
            )
            prefix = self.bot.guild_config.get_prefix(modal_interaction.guild_id)
            await modal_interaction.response.send_message(
                f"✅ Saved as `{save_name}`. Send with `{prefix}embed send {save_name} #channel`.",
                ephemeral=True,
            )
            return True

        view = EmbedBuilderView(ctx.author.id, name=name, on_save=on_save, bot=self.bot)
        view.message = await ctx.send(
            content=(
                f"🧱 Building embed `{name}`. Click a button below to edit a section. "
                f"Preview updates live. Click **✅ Save** to store, then **🔘 Buttons** "
                f"to attach link/role/open/form/ticket/verify buttons."
            ),
            embed=view.state.to_preview_embed(),
            view=view,
        )

    @embed.command(name="save")
    async def save(self, ctx, name: str, *, script: str):
        """Save (or overwrite) an embed from a raw script."""
        if not NAME_RE.match(name):
            return await ctx.send("❌ Name must be 1–32 chars: lowercase letters, digits, `_`, `-`.")
        await self.bot.db.execute(
            """INSERT INTO saved_embeds (guild_id, name, script) VALUES ($1, $2, $3)
               ON CONFLICT (guild_id, name) DO UPDATE SET script = EXCLUDED.script""",
            ctx.guild.id, name, script,
        )
        await ctx.send(f"✅ Saved embed `{name}`.")

    @embed.command(name="list")
    async def list_(self, ctx):
        """List all saved embeds."""
        rows = await self.bot.db.fetch(
            "SELECT name FROM saved_embeds WHERE guild_id=$1 ORDER BY name",
            ctx.guild.id,
        )
        if not rows:
            return await ctx.send("ℹ️ No saved embeds yet.")
        names = ", ".join(f"`{r['name']}`" for r in rows[:100])
        embed = discord.Embed(title="Saved Embeds", description=names, color=discord.Color.blurple())
        embed.set_footer(text=f"{len(rows)} total")
        await ctx.send(embed=embed)

    @embed.command(name="preview")
    async def preview(self, ctx, name: str):
        """Preview a saved embed."""
        script = await fetch_script(self.bot, ctx.guild.id, name)
        if script is None:
            return await ctx.send(f"❌ No embed named `{name}`.")
        rendered = embed_script.render(script, user=ctx.author, guild=ctx.guild, channel=ctx.channel)
        view = await build_view(self.bot, ctx.guild.id, name)
        await ctx.send(
            content=rendered.content or f"_Preview: `{name}`_",
            embed=rendered.embed,
            view=view or discord.utils.MISSING,
        )

    @embed.command(name="send")
    async def send(self, ctx, name: str, channel: discord.TextChannel):
        """Send a saved embed to a channel."""
        script = await fetch_script(self.bot, ctx.guild.id, name)
        if script is None:
            return await ctx.send(f"❌ No embed named `{name}`.")
        rendered = embed_script.render(script, user=ctx.author, guild=ctx.guild, channel=channel)
        view = await build_view(self.bot, ctx.guild.id, name)
        try:
            await channel.send(
                content=rendered.content,
                embed=rendered.embed,
                view=view or discord.utils.MISSING,
            )
        except discord.Forbidden:
            return await ctx.send(f"❌ I can't send in {channel.mention}.")
        await ctx.send(f"✅ Sent `{name}` to {channel.mention}.")

    @embed.command(name="edit")
    async def edit(self, ctx, name: str, *, script: str):
        """Replace the script of a saved embed."""
        existed = await fetch_script(self.bot, ctx.guild.id, name)
        if existed is None:
            return await ctx.send(f"❌ No embed named `{name}`.")
        await self.bot.db.execute(
            "UPDATE saved_embeds SET script=$3 WHERE guild_id=$1 AND name=$2",
            ctx.guild.id, name, script,
        )
        await ctx.send(f"✅ Updated `{name}`.")

    @embed.command(name="delete")
    async def delete(self, ctx, name: str):
        """Delete a saved embed and all its buttons."""
        await self.bot.db.execute(
            "DELETE FROM embed_buttons WHERE guild_id=$1 AND embed_name=$2",
            ctx.guild.id, name,
        )
        result = await self.bot.db.execute(
            "DELETE FROM saved_embeds WHERE guild_id=$1 AND name=$2",
            ctx.guild.id, name,
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        if n == 0:
            return await ctx.send(f"❌ No embed named `{name}`.")
        await ctx.send(f"✅ Deleted `{name}`.")

    @embed.command(name="raw")
    async def raw(self, ctx, name: str):
        """Show the raw script for a saved embed."""
        script = await fetch_script(self.bot, ctx.guild.id, name)
        if script is None:
            return await ctx.send(f"❌ No embed named `{name}`.")
        await ctx.send(f"```\n{script[:1900]}\n```")

    # ---- buttons ----

    @embed.group(name="button", invoke_without_command=True)
    async def button(self, ctx):
        """Manage embed buttons."""
        await self.embed(ctx)

    @button.command(name="addlink")
    async def add_link(self, ctx, name: str, label: str, url: str, emoji: Optional[str] = None):
        """Add a link button to a saved embed. Optional emoji (unicode or <:name:id>)."""
        if await fetch_script(self.bot, ctx.guild.id, name) is None:
            return await ctx.send(f"❌ No embed named `{name}`.")
        await self._add_button(ctx.guild.id, name, "link", "blurple", label, url, emoji=emoji)
        await ctx.send(f"✅ Added link button `{label}` → {url}")

    @button.command(name="addrole")
    async def add_role(self, ctx, name: str, label: str, role: discord.Role, emoji: Optional[str] = None):
        """Add a role-toggle button to a saved embed."""
        if await fetch_script(self.bot, ctx.guild.id, name) is None:
            return await ctx.send(f"❌ No embed named `{name}`.")
        if role >= ctx.guild.me.top_role:
            return await ctx.send("❌ That role is above my highest role.")
        await self._add_button(ctx.guild.id, name, "role", "blurple", label, str(role.id), emoji=emoji)
        await ctx.send(f"✅ Added role-toggle button `{label}` for {role.mention}.")

    @button.command(name="addopen")
    async def add_open(self, ctx, name: str, label: str, target: str, emoji: Optional[str] = None):
        """Add a button that opens another saved embed ephemerally."""
        if await fetch_script(self.bot, ctx.guild.id, name) is None:
            return await ctx.send(f"❌ No embed named `{name}`.")
        if await fetch_script(self.bot, ctx.guild.id, target) is None:
            return await ctx.send(f"❌ No embed named `{target}` to open.")
        await self._add_button(ctx.guild.id, name, "open", "grey", label, target, emoji=emoji)
        await ctx.send(f"✅ Added button `{label}` → opens `{target}` ephemerally.")

    @button.command(name="addform")
    async def add_form(self, ctx, name: str, label: str, form_name: str,
                       color: str = "blurple", emoji: Optional[str] = None):
        """Add a button that opens a form ephemerally."""
        if await fetch_script(self.bot, ctx.guild.id, name) is None:
            return await ctx.send(f"❌ No embed named `{name}`.")
        from cogs.forms import fetch_form
        if await fetch_form(self.bot, ctx.guild.id, form_name) is None:
            return await ctx.send(f"❌ No form `{form_name}`.")
        if color not in COLOR_STYLES:
            return await ctx.send(f"❌ Color must be one of: {', '.join(COLOR_STYLES)}")
        await self._add_button(ctx.guild.id, name, "form", color, label, form_name, emoji=emoji)
        await ctx.send(f"✅ Added form button `{label}` → opens form `{form_name}`.")

    @button.command(name="addticket")
    async def add_ticket(self, ctx, name: str, label: str, panel_name: str,
                         color: str = "blurple", emoji: Optional[str] = None):
        """Add a button that opens a named ticket panel."""
        if await fetch_script(self.bot, ctx.guild.id, name) is None:
            return await ctx.send(f"❌ No embed named `{name}`.")
        panel = await self.bot.db.fetchrow(
            "SELECT 1 FROM ticket_panels WHERE guild_id=$1 AND name=$2",
            ctx.guild.id, panel_name,
        )
        if panel is None:
            return await ctx.send(f"❌ No ticket panel `{panel_name}`.")
        if color not in COLOR_STYLES:
            return await ctx.send(f"❌ Color must be one of: {', '.join(COLOR_STYLES)}")
        await self._add_button(ctx.guild.id, name, "ticket", color, label, panel_name, emoji=emoji)
        await ctx.send(f"✅ Added ticket button `{label}` → panel `{panel_name}`.")

    @button.command(name="addverify")
    async def add_verify(self, ctx, name: str, label: str = "Verify",
                         color: str = "green", emoji: Optional[str] = "✅"):
        """Add a button that starts the verification flow."""
        if await fetch_script(self.bot, ctx.guild.id, name) is None:
            return await ctx.send(f"❌ No embed named `{name}`.")
        if color not in COLOR_STYLES:
            return await ctx.send(f"❌ Color must be one of: {', '.join(COLOR_STYLES)}")
        await self._add_button(ctx.guild.id, name, "verify", color, label, str(ctx.guild.id), emoji=emoji)
        await ctx.send(f"✅ Added verify button `{label}`.")

    @button.command(name="list")
    async def button_list(self, ctx, name: str):
        """List buttons on a saved embed."""
        rows = await self.bot.db.fetch(
            "SELECT position, style, label, target FROM embed_buttons "
            "WHERE guild_id=$1 AND embed_name=$2 ORDER BY position",
            ctx.guild.id, name,
        )
        if not rows:
            return await ctx.send("ℹ️ No buttons on that embed.")
        lines = [f"`{r['position']+1}` · **{r['style']}** · `{r['label']}` → `{r['target']}`" for r in rows]
        await ctx.send("\n".join(lines))

    @button.command(name="remove")
    async def button_remove(self, ctx, name: str, position: int):
        """Remove a button by its position (1-based)."""
        result = await self.bot.db.execute(
            "DELETE FROM embed_buttons WHERE guild_id=$1 AND embed_name=$2 AND position=$3",
            ctx.guild.id, name, position - 1,
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        if n == 0:
            return await ctx.send("❌ No button at that position.")
        await ctx.send("✅ Button removed.")

    async def _add_button(self, guild_id: int, embed_name: str, style: str, color: str,
                          label: str, target: Optional[str], emoji: Optional[str] = None):
        next_pos = await self.bot.db.fetchval(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM embed_buttons "
            "WHERE guild_id=$1 AND embed_name=$2",
            guild_id, embed_name,
        )
        await self.bot.db.execute(
            "INSERT INTO embed_buttons (guild_id, embed_name, position, style, color, label, target, emoji) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            guild_id, embed_name, next_pos, style, color, label, target, emoji,
        )


async def setup(bot):
    await bot.add_cog(Embeds(bot))
