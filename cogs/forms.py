"""Form system — Bleed-style guided forms.

A form is a named set of fields. Each field is either:
  - select  — dropdown with up to 25 predefined options (the user picks one)
  - text    — free-text shown when "Edit Notes" is clicked (single field, optional)

When an embed button of type `form` is clicked:
  1. Bot sends an ephemeral message containing the form's intro embed,
     a select menu for each `select` field, and Continue / Cancel / Edit Notes buttons.
  2. User picks options from each dropdown. Live preview updates with their picks.
  3. Continue posts a formatted submission to the form's target channel,
     with a "Claim" button for staff. Original ephemeral becomes "✅ Submitted."
  4. Cancel disposes the ephemeral.

Configuration is prefix-only — the user creates / edits forms with `.form …`.
The actual button that opens the form is added via `.embed button addform`,
which is wired in cogs/embeds.py separately.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

import discord
from discord.ext import commands

from utils.checks import is_guild_admin

log = logging.getLogger("sentinel.forms")

SCHEMA = """
CREATE TABLE IF NOT EXISTS forms (
    guild_id          BIGINT NOT NULL,
    name              TEXT   NOT NULL,
    title             TEXT   NOT NULL,
    description       TEXT   NOT NULL DEFAULT '',
    color             INTEGER,
    target_channel_id BIGINT,
    notes_enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (guild_id, name)
);

CREATE TABLE IF NOT EXISTS form_fields (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    form_name   TEXT   NOT NULL,
    position    INTEGER NOT NULL,
    label       TEXT   NOT NULL,
    placeholder TEXT,
    options_json TEXT  NOT NULL DEFAULT '[]'  -- JSON array of strings
);

CREATE INDEX IF NOT EXISTS form_fields_lookup
    ON form_fields (guild_id, form_name, position);
"""

NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def _parse_options(options_json: str) -> list[str]:
    try:
        data = json.loads(options_json)
        return [str(x) for x in data][:25]
    except (json.JSONDecodeError, TypeError):
        return []


# ---------------- Submission view (sent to staff channel) ----------------

class SubmissionClaim(discord.ui.DynamicItem[discord.ui.Button],
                      template=r"sentinel:formclaim:(?P<sid>\d+)"):
    def __init__(self, sid: int):
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.success,
                label="Claim",
                emoji="✅",
                custom_id=f"sentinel:formclaim:{sid}",
            )
        )
        self.sid = sid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["sid"]))

    async def callback(self, interaction: discord.Interaction):
        # Mark the button as claimed by editing the message in place.
        try:
            new_view = discord.ui.View(timeout=None)
            new_view.add_item(discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label=f"Claimed by {interaction.user}",
                disabled=True,
                custom_id=f"sentinel:formclaimed:{self.sid}",
            ))
            await interaction.response.edit_message(view=new_view)
        except discord.HTTPException:
            await interaction.response.send_message("✅ Claimed.", ephemeral=True)


# ---------------- Render: form fill view ----------------

class _FormFillView(discord.ui.View):
    """Ephemeral view shown when a user clicks a `form` button on an embed."""

    def __init__(self, bot, form_row: dict, fields: list[dict], invoker_id: int):
        super().__init__(timeout=600)
        self.bot = bot
        self.form_row = form_row
        self.fields = fields
        self.invoker_id = invoker_id
        self.choices: dict[str, str] = {}  # field_label -> chosen option
        self.notes: Optional[str] = None

        # One select per field. Discord allows max 5 components per row, max 5 rows.
        # Selects take a full row. So max 4 selects + the action row.
        for i, fld in enumerate(fields[:4]):
            options = _parse_options(fld["options_json"])
            if not options:
                continue
            select = discord.ui.Select(
                placeholder=fld["placeholder"] or fld["label"],
                options=[discord.SelectOption(label=opt[:100]) for opt in options],
                row=i,
            )
            select._sentinel_field_label = fld["label"]  # type: ignore[attr-defined]
            select.callback = self._make_select_callback(select)
            self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Not yours.", ephemeral=True)
            return False
        return True

    def _make_select_callback(self, select: discord.ui.Select):
        async def cb(interaction: discord.Interaction):
            label = select._sentinel_field_label  # type: ignore[attr-defined]
            self.choices[label] = select.values[0]
            await interaction.response.edit_message(embed=self._preview(), view=self)
        return cb

    def _preview(self) -> discord.Embed:
        e = discord.Embed(
            title=self.form_row['title'],
            description=self.form_row["description"] or "Fill in the options below.",
            color=discord.Color(self.form_row["color"]) if self.form_row["color"] else discord.Color.blurple(),
        )
        lines = []
        for fld in self.fields:
            chosen = self.choices.get(fld["label"], "_(not set)_")
            lines.append(f"**{fld['label']}**: {chosen}")
        if self.form_row["notes_enabled"]:
            note_preview = self.notes if self.notes else "_(none)_"
            lines.append(f"**Notes**: {note_preview}")
        e.add_field(name="Your selections", value="\n".join(lines) or "_no fields_", inline=False)
        return e

    @discord.ui.button(label="Edit Notes", style=discord.ButtonStyle.secondary, emoji="✏", row=4)
    async def edit_notes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.form_row["notes_enabled"]:
            return await interaction.response.send_message("Notes disabled for this form.", ephemeral=True)
        await interaction.response.send_modal(_NotesModal(self))

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.success, emoji="✅", row=4)
    async def continue_(self, interaction: discord.Interaction, button: discord.ui.Button):
        # All required selects must have a choice.
        missing = [f["label"] for f in self.fields if f["label"] not in self.choices]
        if missing:
            return await interaction.response.send_message(
                f"❌ Pick: {', '.join(missing)}", ephemeral=True,
            )
        if not self.form_row["target_channel_id"]:
            return await interaction.response.send_message(
                "❌ Form is missing a target channel — staff hasn't set one.", ephemeral=True,
            )
        target = interaction.guild.get_channel(self.form_row["target_channel_id"])
        if target is None:
            return await interaction.response.send_message(
                "❌ Target channel no longer exists.", ephemeral=True,
            )

        # Persist submission and grab its ID.
        sid = await self.bot.db.fetchval(
            """INSERT INTO form_submissions
               (guild_id, form_name, submitter_id, choices_json, notes)
               VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            interaction.guild_id, self.form_row["name"], interaction.user.id,
            json.dumps(self.choices), self.notes,
        )

        embed = discord.Embed(
            title=f"📝 {self.form_row['title']} — Submission",
            color=discord.Color(self.form_row["color"]) if self.form_row["color"] else discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(
            name=str(interaction.user), icon_url=interaction.user.display_avatar.url,
        )
        for fld in self.fields:
            embed.add_field(name=fld["label"], value=self.choices.get(fld["label"], "—"), inline=True)
        if self.form_row["notes_enabled"] and self.notes:
            embed.add_field(name="Notes", value=self.notes[:1024], inline=False)
        embed.set_footer(text=f"Submission #{sid}")

        view = discord.ui.View(timeout=None)
        view.add_item(SubmissionClaim(sid))

        try:
            await target.send(content=f"<@&{self.form_row.get('staff_role_id') or ''}>".strip("<@&>") if False else None, embed=embed, view=view)
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ I can't post to the target channel — missing permission.", ephemeral=True,
            )

        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(
            content=f"✅ Submitted. Staff will review it shortly.",
            embed=None, view=self,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="✖", row=4)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(
            content="❌ Cancelled.", embed=None, view=self,
        )


class _NotesModal(discord.ui.Modal, title="Notes"):
    notes_input = discord.ui.TextInput(
        label="Notes (optional)", style=discord.TextStyle.long, required=False, max_length=1000,
    )

    def __init__(self, fill_view: _FormFillView):
        super().__init__()
        self.fill_view = fill_view
        self.notes_input.default = fill_view.notes or ""

    async def on_submit(self, interaction: discord.Interaction):
        self.fill_view.notes = (self.notes_input.value or "").strip() or None
        await interaction.response.edit_message(embed=self.fill_view._preview(), view=self.fill_view)


# ---------------- Public render entry-point (called by embed `form` button) ----------------

async def fetch_form(bot, guild_id: int, name: str) -> Optional[dict]:
    row = await bot.db.fetchrow(
        "SELECT * FROM forms WHERE guild_id=$1 AND name=$2", guild_id, name,
    )
    return dict(row) if row else None


async def fetch_form_fields(bot, guild_id: int, name: str) -> list[dict]:
    rows = await bot.db.fetch(
        "SELECT * FROM form_fields WHERE guild_id=$1 AND form_name=$2 ORDER BY position",
        guild_id, name,
    )
    return [dict(r) for r in rows]


async def render_form_for(bot, interaction: discord.Interaction, form_name: str):
    """Called when an embed button of type `form` is clicked."""
    form = await fetch_form(bot, interaction.guild_id, form_name)
    if form is None:
        return await interaction.response.send_message(
            f"❌ Form `{form_name}` no longer exists.", ephemeral=True,
        )
    fields = await fetch_form_fields(bot, interaction.guild_id, form_name)
    if not fields:
        return await interaction.response.send_message(
            "❌ This form has no fields configured yet.", ephemeral=True,
        )
    view = _FormFillView(bot, form, fields, interaction.user.id)
    await interaction.response.send_message(embed=view._preview(), view=view, ephemeral=True)


# ---------------- Cog ----------------

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS form_submissions (
    id            BIGSERIAL PRIMARY KEY,
    guild_id      BIGINT NOT NULL,
    form_name     TEXT NOT NULL,
    submitter_id  BIGINT NOT NULL,
    choices_json  TEXT NOT NULL,
    notes         TEXT,
    submitted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class Forms(commands.Cog):
    """📋 Forms"""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)
        await self.bot.db.execute(EXTRA_SCHEMA)
        self.bot.add_dynamic_items(SubmissionClaim)

    @commands.group(name="form", aliases=["fm"], invoke_without_command=True)
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def form(self, ctx):
        """Forms — guided ephemeral data-collection embeds."""
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        await ctx.send(
            f"**Forms**\n"
            f"`{prefix}form create <name> <title>` · create a new form\n"
            f"`{prefix}form description <name> <text>`\n"
            f"`{prefix}form color <name> <hex>`\n"
            f"`{prefix}form target <name> <#channel>` · where submissions post\n"
            f"`{prefix}form notes <name> on|off` · enable/disable Notes button\n"
            f"`{prefix}form addselect <name> <label> | opt1 | opt2 | opt3 …`\n"
            f"`{prefix}form removefield <name> <position>`\n"
            f"`{prefix}form list` · `{prefix}form view <name>` · `{prefix}form delete <name>`\n"
            f"\nThen attach to an embed with `{prefix}embed button addform <embed> <label> <form>`.",
        )

    @form.command(name="create")
    async def create(self, ctx, name: str, *, title: str):
        """Create a new form."""
        if not NAME_RE.match(name):
            return await ctx.send("❌ Name must be 1–32 chars: lowercase letters, digits, `_`, `-`.")
        try:
            await self.bot.db.execute(
                "INSERT INTO forms (guild_id, name, title) VALUES ($1, $2, $3)",
                ctx.guild.id, name, title,
            )
        except Exception as e:
            return await ctx.send(f"❌ Failed (already exists?): {e}")
        await ctx.send(f"✅ Form `{name}` created. Add fields with `form addselect`.")

    @form.command(name="description")
    async def description(self, ctx, name: str, *, text: str):
        """Set the form's description (intro text shown above the dropdowns)."""
        await self._update(ctx, name, description=text)

    @form.command(name="color")
    async def color(self, ctx, name: str, hex_color: str):
        """Set the form's accent color (hex)."""
        m = re.fullmatch(r"#?([0-9a-fA-F]{6})", hex_color.strip())
        if m is None:
            return await ctx.send("❌ Hex like `#5865F2`.")
        await self._update(ctx, name, color=int(m.group(1), 16))

    @form.command(name="target")
    async def target(self, ctx, name: str, channel: discord.TextChannel):
        """Set the channel where submissions are posted."""
        await self._update(ctx, name, target_channel_id=channel.id)

    @form.command(name="notes")
    async def notes(self, ctx, name: str, state: str):
        """Enable or disable the Notes button on this form (on/off)."""
        if state.lower() not in ("on", "off"):
            return await ctx.send("❌ Use `on` or `off`.")
        await self._update(ctx, name, notes_enabled=(state.lower() == "on"))

    @form.command(name="addselect")
    async def addselect(self, ctx, name: str, *, body: str):
        """Add a select field. Format: <label> | <opt1> | <opt2> | <opt3>…"""
        if await fetch_form(self.bot, ctx.guild.id, name) is None:
            return await ctx.send(f"❌ No form `{name}`.")
        parts = [p.strip() for p in body.split("|")]
        if len(parts) < 3:
            return await ctx.send("❌ Need a label and at least 2 options. Format: `label | opt1 | opt2 | …`")
        label, *options = parts
        if len(options) > 25:
            return await ctx.send("❌ Max 25 options.")
        existing = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM form_fields WHERE guild_id=$1 AND form_name=$2",
            ctx.guild.id, name,
        )
        if existing >= 4:
            return await ctx.send("❌ Max 4 select fields per form (Discord limit).")
        next_pos = await self.bot.db.fetchval(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM form_fields "
            "WHERE guild_id=$1 AND form_name=$2",
            ctx.guild.id, name,
        )
        await self.bot.db.execute(
            """INSERT INTO form_fields
               (guild_id, form_name, position, label, options_json)
               VALUES ($1, $2, $3, $4, $5)""",
            ctx.guild.id, name, next_pos, label, json.dumps(options),
        )
        await ctx.send(f"✅ Added select `{label}` with {len(options)} options.")

    @form.command(name="removefield")
    async def removefield(self, ctx, name: str, position: int):
        """Remove a field by position (1-based)."""
        result = await self.bot.db.execute(
            "DELETE FROM form_fields WHERE guild_id=$1 AND form_name=$2 AND position=$3",
            ctx.guild.id, name, position - 1,
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        await ctx.send(f"✅ Removed {n} field(s).")

    @form.command(name="list")
    async def list_(self, ctx):
        """List forms."""
        rows = await self.bot.db.fetch(
            "SELECT name, title, target_channel_id FROM forms WHERE guild_id=$1 ORDER BY name",
            ctx.guild.id,
        )
        if not rows:
            return await ctx.send("ℹ️ No forms.")
        lines = []
        for r in rows:
            ch = ctx.guild.get_channel(r["target_channel_id"]) if r["target_channel_id"] else None
            lines.append(f"`{r['name']}` — {r['title']} → {ch.mention if ch else '_no target_'}")
        await ctx.send("\n".join(lines))

    @form.command(name="view")
    async def view(self, ctx, name: str):
        """Show config + fields for a form."""
        form = await fetch_form(self.bot, ctx.guild.id, name)
        if form is None:
            return await ctx.send(f"❌ No form `{name}`.")
        fields = await fetch_form_fields(self.bot, ctx.guild.id, name)
        ch = ctx.guild.get_channel(form["target_channel_id"]) if form["target_channel_id"] else None
        embed = discord.Embed(
            title=f"Form: {form['name']}",
            description=form["description"] or "_(no description)_",
            color=discord.Color(form["color"]) if form["color"] else discord.Color.blurple(),
        )
        embed.add_field(name="Title", value=form["title"], inline=True)
        embed.add_field(name="Target", value=ch.mention if ch else "—", inline=True)
        embed.add_field(name="Notes", value="enabled" if form["notes_enabled"] else "disabled", inline=True)
        for fld in fields:
            opts = _parse_options(fld["options_json"])
            embed.add_field(
                name=f"{fld['position']+1}. {fld['label']}",
                value=", ".join(f"`{o}`" for o in opts) or "_no options_",
                inline=False,
            )
        await ctx.send(embed=embed)

    @form.command(name="delete")
    async def delete(self, ctx, name: str):
        """Delete a form (and its fields)."""
        await self.bot.db.execute(
            "DELETE FROM form_fields WHERE guild_id=$1 AND form_name=$2", ctx.guild.id, name,
        )
        result = await self.bot.db.execute(
            "DELETE FROM forms WHERE guild_id=$1 AND name=$2", ctx.guild.id, name,
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        if n == 0:
            return await ctx.send(f"❌ No form `{name}`.")
        await ctx.send(f"✅ Deleted `{name}`.")

    async def _update(self, ctx, name: str, **fields):
        if await fetch_form(self.bot, ctx.guild.id, name) is None:
            return await ctx.send(f"❌ No form `{name}`.")
        sets = ", ".join(f"{k}=${i+3}" for i, k in enumerate(fields))
        await self.bot.db.execute(
            f"UPDATE forms SET {sets} WHERE guild_id=$1 AND name=$2",
            ctx.guild.id, name, *fields.values(),
        )
        await ctx.send(f"✅ Updated `{name}`.")


async def setup(bot):
    await bot.add_cog(Forms(bot))
