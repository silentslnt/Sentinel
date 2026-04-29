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
from discord import app_commands
from discord.ext import commands

from utils import embed_script

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
    style      TEXT NOT NULL,  -- 'link' | 'role' | 'open' | 'plain'
    color      TEXT NOT NULL DEFAULT 'blurple',
    label      TEXT NOT NULL,
    target     TEXT
);

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
    def __init__(self, role_id: int, label: str = "Role", style: discord.ButtonStyle = discord.ButtonStyle.primary):
        super().__init__(
            discord.ui.Button(
                style=style,
                label=label,
                custom_id=f"sentinel:roletoggle:{role_id}",
            )
        )
        self.role_id = role_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(int(match["role_id"]), label=item.label or "Role", style=item.style)

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
    def __init__(self, name: str, label: str = "Open", style: discord.ButtonStyle = discord.ButtonStyle.secondary):
        super().__init__(
            discord.ui.Button(
                style=style,
                label=label,
                custom_id=f"sentinel:embedopen:{name}",
            )
        )
        self.name = name

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(match["name"], label=item.label or "Open", style=item.style)

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
        if r["style"] == "link":
            view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label=r["label"], url=r["target"]))
        elif r["style"] == "role":
            try:
                view.add_item(RoleToggleButton(int(r["target"]), label=r["label"], style=style_color))
            except (TypeError, ValueError):
                continue
        elif r["style"] == "open":
            view.add_item(OpenEmbedButton(r["target"], label=r["label"], style=style_color))
        else:  # plain (decorative)
            view.add_item(discord.ui.Button(style=style_color, label=r["label"], disabled=True,
                                            custom_id=f"sentinel:plain:{guild_id}:{embed_name}:{r['position']}"))
    return view


async def fetch_script(bot, guild_id: int, name: str) -> Optional[str]:
    return await bot.db.fetchval(
        "SELECT script FROM saved_embeds WHERE guild_id=$1 AND name=$2",
        guild_id, name,
    )


# ---------------- Modal for /embed create ----------------

class EmbedCreateModal(discord.ui.Modal, title="Create Embed"):
    embed_title = discord.ui.TextInput(label="Title", required=False, max_length=256)
    description = discord.ui.TextInput(label="Description", style=discord.TextStyle.long, required=False, max_length=4000)
    color = discord.ui.TextInput(label="Color (hex, e.g. 5865F2)", required=False, max_length=7)
    footer = discord.ui.TextInput(label="Footer", required=False, max_length=2048)
    image = discord.ui.TextInput(label="Image URL", required=False)

    def __init__(self, name: str):
        super().__init__()
        self.name = name

    async def on_submit(self, interaction: discord.Interaction):
        parts = []
        if self.embed_title.value:
            parts.append(f"{{title: {self.embed_title.value}}}")
        if self.description.value:
            parts.append(f"{{description: {self.description.value}}}")
        if self.color.value:
            parts.append(f"{{color: #{self.color.value.lstrip('#')}}}")
        if self.footer.value:
            parts.append(f"{{footer: {self.footer.value}}}")
        if self.image.value:
            parts.append(f"{{image: {self.image.value}}}")
        if not parts:
            return await interaction.response.send_message("❌ All fields empty — nothing to save.", ephemeral=True)
        script = "$v".join(parts)

        await interaction.client.db.execute(
            """INSERT INTO saved_embeds (guild_id, name, script) VALUES ($1, $2, $3)
               ON CONFLICT (guild_id, name) DO UPDATE SET script = EXCLUDED.script""",
            interaction.guild_id, self.name, script,
        )
        rendered = embed_script.render(script, user=interaction.user, guild=interaction.guild, channel=interaction.channel)
        await interaction.response.send_message(
            content=f"✅ Saved embed `{self.name}`. Preview:" + (f"\n{rendered.content}" if rendered.content else ""),
            embed=rendered.embed,
            ephemeral=True,
        )


# ---------------- Cog ----------------

class Embeds(commands.Cog):
    """🧱 Embed builder & library"""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)
        # Register dynamic item handlers globally so saved embeds keep working after restarts.
        self.bot.add_dynamic_items(RoleToggleButton, OpenEmbedButton)

    embed = app_commands.Group(
        name="embed",
        description="Embed builder",
        default_permissions=discord.Permissions(manage_messages=True),
        guild_only=True,
    )

    button = app_commands.Group(name="button", description="Manage embed buttons", parent=embed)

    @embed.command(name="create", description="Open an interactive modal to create a saved embed")
    @app_commands.describe(name="Short name (lowercase letters, digits, _ and - only)")
    async def create(self, interaction: discord.Interaction, name: str):
        if not NAME_RE.match(name):
            return await interaction.response.send_message(
                "❌ Name must be 1–32 chars: lowercase letters, digits, `_`, `-`.", ephemeral=True,
            )
        await interaction.response.send_modal(EmbedCreateModal(name))

    @embed.command(name="save", description="Save (or overwrite) an embed from a script")
    @app_commands.describe(
        name="Short name",
        script="Embed script (e.g. {title: hi}$v{description: hello})",
    )
    async def save(self, interaction: discord.Interaction, name: str, script: str):
        if not NAME_RE.match(name):
            return await interaction.response.send_message(
                "❌ Name must be 1–32 chars: lowercase letters, digits, `_`, `-`.", ephemeral=True,
            )
        await self.bot.db.execute(
            """INSERT INTO saved_embeds (guild_id, name, script) VALUES ($1, $2, $3)
               ON CONFLICT (guild_id, name) DO UPDATE SET script = EXCLUDED.script""",
            interaction.guild_id, name, script,
        )
        await interaction.response.send_message(f"✅ Saved embed `{name}`.", ephemeral=True)

    @embed.command(name="list", description="List all saved embeds")
    async def list_(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetch(
            "SELECT name FROM saved_embeds WHERE guild_id=$1 ORDER BY name",
            interaction.guild_id,
        )
        if not rows:
            return await interaction.response.send_message("ℹ️ No saved embeds yet.", ephemeral=True)
        names = ", ".join(f"`{r['name']}`" for r in rows[:100])
        embed = discord.Embed(title="Saved Embeds", description=names, color=discord.Color.blurple())
        embed.set_footer(text=f"{len(rows)} total")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @embed.command(name="preview", description="Preview a saved embed (only you can see it)")
    async def preview(self, interaction: discord.Interaction, name: str):
        script = await fetch_script(self.bot, interaction.guild_id, name)
        if script is None:
            return await interaction.response.send_message(f"❌ No embed named `{name}`.", ephemeral=True)
        rendered = embed_script.render(script, user=interaction.user, guild=interaction.guild, channel=interaction.channel)
        view = await build_view(self.bot, interaction.guild_id, name)
        await interaction.response.send_message(
            content=rendered.content or f"_Preview: `{name}`_",
            embed=rendered.embed,
            view=view or discord.utils.MISSING,
            ephemeral=True,
        )

    @embed.command(name="send", description="Send a saved embed to a channel")
    async def send(self, interaction: discord.Interaction, name: str, channel: discord.TextChannel):
        script = await fetch_script(self.bot, interaction.guild_id, name)
        if script is None:
            return await interaction.response.send_message(f"❌ No embed named `{name}`.", ephemeral=True)
        rendered = embed_script.render(script, user=interaction.user, guild=interaction.guild, channel=channel)
        view = await build_view(self.bot, interaction.guild_id, name)
        try:
            await channel.send(
                content=rendered.content,
                embed=rendered.embed,
                view=view or discord.utils.MISSING,
            )
        except discord.Forbidden:
            return await interaction.response.send_message(f"❌ I can't send in {channel.mention}.", ephemeral=True)
        await interaction.response.send_message(f"✅ Sent `{name}` to {channel.mention}.", ephemeral=True)

    @embed.command(name="edit", description="Replace the script of a saved embed")
    async def edit(self, interaction: discord.Interaction, name: str, script: str):
        existed = await fetch_script(self.bot, interaction.guild_id, name)
        if existed is None:
            return await interaction.response.send_message(f"❌ No embed named `{name}`.", ephemeral=True)
        await self.bot.db.execute(
            "UPDATE saved_embeds SET script=$3 WHERE guild_id=$1 AND name=$2",
            interaction.guild_id, name, script,
        )
        await interaction.response.send_message(f"✅ Updated `{name}`.", ephemeral=True)

    @embed.command(name="delete", description="Delete a saved embed and all its buttons")
    async def delete(self, interaction: discord.Interaction, name: str):
        await self.bot.db.execute(
            "DELETE FROM embed_buttons WHERE guild_id=$1 AND embed_name=$2",
            interaction.guild_id, name,
        )
        result = await self.bot.db.execute(
            "DELETE FROM saved_embeds WHERE guild_id=$1 AND name=$2",
            interaction.guild_id, name,
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        if n == 0:
            return await interaction.response.send_message(f"❌ No embed named `{name}`.", ephemeral=True)
        await interaction.response.send_message(f"✅ Deleted `{name}`.", ephemeral=True)

    @embed.command(name="raw", description="Show the raw script for a saved embed")
    async def raw(self, interaction: discord.Interaction, name: str):
        script = await fetch_script(self.bot, interaction.guild_id, name)
        if script is None:
            return await interaction.response.send_message(f"❌ No embed named `{name}`.", ephemeral=True)
        await interaction.response.send_message(f"```\n{script[:1900]}\n```", ephemeral=True)

    # ---- buttons ----

    @button.command(name="addlink", description="Add a link button to a saved embed")
    @app_commands.describe(name="Embed name", label="Button label", url="URL to open")
    async def add_link(self, interaction: discord.Interaction, name: str, label: str, url: str):
        if await fetch_script(self.bot, interaction.guild_id, name) is None:
            return await interaction.response.send_message(f"❌ No embed named `{name}`.", ephemeral=True)
        await self._add_button(interaction.guild_id, name, "link", "blurple", label, url)
        await interaction.response.send_message(f"✅ Added link button `{label}` → {url}", ephemeral=True)

    @button.command(name="addrole", description="Add a role-toggle button to a saved embed")
    @app_commands.describe(name="Embed name", label="Button label", role="Role to toggle on click")
    async def add_role(self, interaction: discord.Interaction, name: str, label: str, role: discord.Role):
        if await fetch_script(self.bot, interaction.guild_id, name) is None:
            return await interaction.response.send_message(f"❌ No embed named `{name}`.", ephemeral=True)
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(
                "❌ That role is above my highest role.", ephemeral=True,
            )
        await self._add_button(interaction.guild_id, name, "role", "blurple", label, str(role.id))
        await interaction.response.send_message(
            f"✅ Added role-toggle button `{label}` for {role.mention}.", ephemeral=True,
        )

    @button.command(name="addopen", description="Add a button that opens another saved embed ephemerally")
    @app_commands.describe(name="Embed name (the one you're adding the button to)", label="Button label",
                           target="Name of the embed to open when clicked")
    async def add_open(self, interaction: discord.Interaction, name: str, label: str, target: str):
        if await fetch_script(self.bot, interaction.guild_id, name) is None:
            return await interaction.response.send_message(f"❌ No embed named `{name}`.", ephemeral=True)
        if await fetch_script(self.bot, interaction.guild_id, target) is None:
            return await interaction.response.send_message(f"❌ No embed named `{target}` to open.", ephemeral=True)
        await self._add_button(interaction.guild_id, name, "open", "grey", label, target)
        await interaction.response.send_message(
            f"✅ Added button `{label}` → opens `{target}` ephemerally.", ephemeral=True,
        )

    @button.command(name="list", description="List buttons on a saved embed")
    async def button_list(self, interaction: discord.Interaction, name: str):
        rows = await self.bot.db.fetch(
            "SELECT position, style, label, target FROM embed_buttons "
            "WHERE guild_id=$1 AND embed_name=$2 ORDER BY position",
            interaction.guild_id, name,
        )
        if not rows:
            return await interaction.response.send_message("ℹ️ No buttons on that embed.", ephemeral=True)
        lines = [f"`{r['position']+1}` · **{r['style']}** · `{r['label']}` → `{r['target']}`" for r in rows]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @button.command(name="remove", description="Remove a button by its position (1-based)")
    async def button_remove(self, interaction: discord.Interaction, name: str, position: int):
        result = await self.bot.db.execute(
            "DELETE FROM embed_buttons WHERE guild_id=$1 AND embed_name=$2 AND position=$3",
            interaction.guild_id, name, position - 1,
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        if n == 0:
            return await interaction.response.send_message("❌ No button at that position.", ephemeral=True)
        await interaction.response.send_message("✅ Button removed.", ephemeral=True)

    async def _add_button(self, guild_id: int, embed_name: str, style: str, color: str,
                          label: str, target: Optional[str]):
        next_pos = await self.bot.db.fetchval(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM embed_buttons "
            "WHERE guild_id=$1 AND embed_name=$2",
            guild_id, embed_name,
        )
        await self.bot.db.execute(
            "INSERT INTO embed_buttons (guild_id, embed_name, position, style, color, label, target) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            guild_id, embed_name, next_pos, style, color, label, target,
        )


async def setup(bot):
    await bot.add_cog(Embeds(bot))
