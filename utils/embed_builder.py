"""Guided embed builder UI (Bleed-style, but interactive).

Usage from a slash command:

    builder = EmbedBuilderView(interaction.user.id, name=name, on_save=save_callback)
    await interaction.response.send_message(
        content="Building embed `{name}`. Click a button to edit a section.",
        embed=builder.state.to_preview_embed(),
        view=builder,
        ephemeral=False,
    )
    builder.message = await interaction.original_response()

`on_save` is an async callable: `async def(interaction, name, script) -> bool`.
It receives the modal's submit-interaction so it can respond to the user, plus
the chosen name and the serialized embed-script string. Return True on success.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import discord


# ------------------- state -------------------

@dataclass
class EmbedBuilderState:
    title: Optional[str] = None
    description: Optional[str] = None
    url: Optional[str] = None
    color: Optional[int] = 0xFFFFFF  # white by default
    author_name: Optional[str] = None
    author_icon: Optional[str] = None
    author_url: Optional[str] = None
    footer_text: Optional[str] = None
    footer_icon: Optional[str] = None
    image_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    # list of (name, value, inline)
    fields: list[tuple[str, str, bool]] = field(default_factory=list)

    def to_preview_embed(self) -> discord.Embed:
        """Render the current state. Always returns *something* so the preview shows."""
        if not any([
            self.title, self.description, self.author_name, self.footer_text,
            self.image_url, self.thumbnail_url, self.fields,
        ]):
            return discord.Embed(
                description="_Empty embed — use the buttons below to add content._",
                color=discord.Color.dark_grey(),
            )
        e = discord.Embed()
        if self.title:
            e.title = self.title[:256]
        if self.description:
            e.description = self.description[:4096]
        if self.url:
            e.url = self.url
        if self.color is not None:
            e.color = discord.Color(self.color)
        if self.author_name:
            e.set_author(
                name=self.author_name[:256],
                icon_url=self.author_icon or None,
                url=self.author_url or None,
            )
        if self.footer_text:
            e.set_footer(text=self.footer_text[:2048], icon_url=self.footer_icon or None)
        if self.image_url:
            e.set_image(url=self.image_url)
        if self.thumbnail_url:
            e.set_thumbnail(url=self.thumbnail_url)
        for name, value, inline in self.fields:
            e.add_field(name=name[:256], value=value[:1024], inline=inline)
        return e

    def to_script(self) -> str:
        parts = []
        if self.title:
            parts.append(f"{{title: {self.title}}}")
        if self.description:
            parts.append(f"{{description: {self.description}}}")
        if self.url:
            parts.append(f"{{url: {self.url}}}")
        if self.color is not None:
            parts.append(f"{{color: #{self.color:06x}}}")
        if self.author_name:
            chunks = [self.author_name]
            if self.author_icon:
                chunks.append(self.author_icon)
            if self.author_url:
                chunks.append(self.author_url)
            parts.append(f"{{author: {' && '.join(chunks)}}}")
        if self.footer_text:
            chunks = [self.footer_text]
            if self.footer_icon:
                chunks.append(self.footer_icon)
            parts.append(f"{{footer: {' && '.join(chunks)}}}")
        if self.image_url:
            parts.append(f"{{image: {self.image_url}}}")
        if self.thumbnail_url:
            parts.append(f"{{thumbnail: {self.thumbnail_url}}}")
        for name, value, inline in self.fields:
            chunk = f"{name} && {value}"
            if inline:
                chunk += " && inline"
            parts.append(f"{{field: {chunk}}}")
        return "$v".join(parts)


# ------------------- modals -------------------

def _opt(s: Optional[str]) -> Optional[str]:
    """Treat empty input as 'unset'."""
    s = (s or "").strip()
    return s or None


class _TitleModal(discord.ui.Modal, title="Title"):
    title_field = discord.ui.TextInput(label="Title", required=False, max_length=256)
    url_field = discord.ui.TextInput(label="URL (clickable on title)", required=False)

    def __init__(self, view: "EmbedBuilderView"):
        super().__init__()
        self._view = view
        self.title_field.default = view.state.title or ""
        self.url_field.default = view.state.url or ""

    async def on_submit(self, interaction: discord.Interaction):
        self._view.state.title = _opt(self.title_field.value)
        self._view.state.url = _opt(self.url_field.value)
        await self._view._refresh(interaction)


class _DescriptionModal(discord.ui.Modal, title="Description"):
    desc_field = discord.ui.TextInput(label="Description", style=discord.TextStyle.long, required=False, max_length=4000)

    def __init__(self, view: "EmbedBuilderView"):
        super().__init__()
        self._view = view
        self.desc_field.default = view.state.description or ""

    async def on_submit(self, interaction: discord.Interaction):
        self._view.state.description = _opt(self.desc_field.value)
        await self._view._refresh(interaction)


class _AuthorModal(discord.ui.Modal, title="Author"):
    name_field = discord.ui.TextInput(label="Name", required=False, max_length=256)
    icon_field = discord.ui.TextInput(label="Icon URL", required=False)
    url_field = discord.ui.TextInput(label="Click URL", required=False)

    def __init__(self, view: "EmbedBuilderView"):
        super().__init__()
        self._view = view
        self.name_field.default = view.state.author_name or ""
        self.icon_field.default = view.state.author_icon or ""
        self.url_field.default = view.state.author_url or ""

    async def on_submit(self, interaction: discord.Interaction):
        self._view.state.author_name = _opt(self.name_field.value)
        self._view.state.author_icon = _opt(self.icon_field.value)
        self._view.state.author_url = _opt(self.url_field.value)
        await self._view._refresh(interaction)


class _FooterModal(discord.ui.Modal, title="Footer"):
    text_field = discord.ui.TextInput(label="Text", required=False, max_length=2048)
    icon_field = discord.ui.TextInput(label="Icon URL", required=False)

    def __init__(self, view: "EmbedBuilderView"):
        super().__init__()
        self._view = view
        self.text_field.default = view.state.footer_text or ""
        self.icon_field.default = view.state.footer_icon or ""

    async def on_submit(self, interaction: discord.Interaction):
        self._view.state.footer_text = _opt(self.text_field.value)
        self._view.state.footer_icon = _opt(self.icon_field.value)
        await self._view._refresh(interaction)


class _ImageModal(discord.ui.Modal):
    url_field = discord.ui.TextInput(label="Image URL", required=False)

    def __init__(self, view: "EmbedBuilderView", *, kind: str):
        super().__init__(title=f"{kind.capitalize()} Image")
        self._view = view
        self._kind = kind  # "image" or "thumbnail"
        current = view.state.image_url if kind == "image" else view.state.thumbnail_url
        self.url_field.default = current or ""

    async def on_submit(self, interaction: discord.Interaction):
        url = _opt(self.url_field.value)
        if self._kind == "image":
            self._view.state.image_url = url
        else:
            self._view.state.thumbnail_url = url
        await self._view._refresh(interaction)


class _CustomHexModal(discord.ui.Modal, title="Custom Hex Color"):
    hex_field = discord.ui.TextInput(label="Hex (e.g. 5865F2)", min_length=6, max_length=7)

    def __init__(self, view: "EmbedBuilderView"):
        super().__init__()
        self._view = view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            self._view.state.color = int(self.hex_field.value.lstrip("#"), 16)
        except ValueError:
            return await interaction.response.send_message("❌ Invalid hex.", ephemeral=True)
        await self._view._refresh(interaction)


class _AddFieldModal(discord.ui.Modal, title="Add Field"):
    name_field = discord.ui.TextInput(label="Name", max_length=256)
    value_field = discord.ui.TextInput(label="Value", style=discord.TextStyle.long, max_length=1024)
    inline_field = discord.ui.TextInput(label="Inline? (yes/no)", default="no", max_length=3)

    def __init__(self, view: "EmbedBuilderView"):
        super().__init__()
        self._view = view

    async def on_submit(self, interaction: discord.Interaction):
        if len(self._view.state.fields) >= 25:
            return await interaction.response.send_message("❌ Already at 25 fields.", ephemeral=True)
        inline = self.inline_field.value.strip().lower() in ("yes", "y", "true", "1", "inline")
        self._view.state.fields.append((self.name_field.value, self.value_field.value, inline))
        await self._view._refresh(interaction)


class _SaveModal(discord.ui.Modal, title="Save Embed"):
    name_field = discord.ui.TextInput(label="Save as (a-z, 0-9, _, -)", max_length=32)

    def __init__(self, view: "EmbedBuilderView"):
        super().__init__()
        self._view = view
        if view.name:
            self.name_field.default = view.name

    async def on_submit(self, interaction: discord.Interaction):
        name = self.name_field.value.strip().lower()
        if not name or not all(c.isalnum() or c in "_-" for c in name):
            return await interaction.response.send_message(
                "❌ Name must be a-z, 0-9, `_`, `-` (max 32).", ephemeral=True,
            )
        if self._view.on_save is None:
            return await interaction.response.send_message("❌ No save callback wired.", ephemeral=True)
        script = self._view.state.to_script()
        if not script:
            return await interaction.response.send_message("❌ Embed is empty — add some content first.", ephemeral=True)
        ok = await self._view.on_save(interaction, name, script)
        if ok:
            self._view.name = name
            self._view._saved = True
            # Don't disable the view — let the user keep editing or jump to button management.
            try:
                await self._view.message.edit(
                    content=f"✅ Saved as `{name}`. You can keep editing, click **🔘 Buttons** to attach buttons, or **🗑 Discard** to close.",
                    view=self._view,
                )
            except (discord.NotFound, discord.HTTPException):
                pass


# ------------------- color sub-view -------------------

PRESET_COLORS = [
    ("Red",     0xED4245),
    ("Orange",  0xF1652A),
    ("Yellow",  0xFEE75C),
    ("Green",   0x57F287),
    ("Blue",    0x3498DB),
    ("Blurple", 0x5865F2),
    ("Purple",  0x9B59B6),
    ("Pink",    0xEB459E),
]


class _PresetColorButton(discord.ui.Button):
    def __init__(self, label: str, color: int, builder_view: "EmbedBuilderView"):
        super().__init__(style=discord.ButtonStyle.secondary, label=label)
        self.color_value = color
        self.builder_view = builder_view

    async def callback(self, interaction: discord.Interaction):
        self.builder_view.state.color = self.color_value
        await self.builder_view._refresh(interaction, also_close_self=True)


class _CustomHexButton(discord.ui.Button):
    def __init__(self, builder_view: "EmbedBuilderView"):
        super().__init__(style=discord.ButtonStyle.primary, label="Custom Hex")
        self.builder_view = builder_view

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_CustomHexModal(self.builder_view))


class _ColorPickerView(discord.ui.View):
    def __init__(self, builder_view: "EmbedBuilderView"):
        super().__init__(timeout=120)
        for label, value in PRESET_COLORS:
            self.add_item(_PresetColorButton(label, value, builder_view))
        self.add_item(_CustomHexButton(builder_view))


# ------------------- main view -------------------

OnSaveCb = Callable[[discord.Interaction, str, str], Awaitable[bool]]


class EmbedBuilderView(discord.ui.View):
    def __init__(
        self,
        author_id: int,
        *,
        name: Optional[str] = None,
        on_save: Optional[OnSaveCb] = None,
        bot=None,
        timeout: float = 900.0,
    ):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.name = name
        self.on_save = on_save
        self.bot = bot  # needed for the Buttons sub-view to write to embed_buttons
        self.state = EmbedBuilderState()
        self.message: Optional[discord.Message] = None
        self._saved = False  # set to True after the first successful save

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the builder owner can edit this.", ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction, *, also_close_self: bool = False):
        """Update the builder message in place after a state change."""
        embed = self.state.to_preview_embed()
        if also_close_self:
            # Came from the color sub-view (separate ephemeral). Edit the original
            # builder message via stored handle, and dismiss the ephemeral.
            try:
                if self.message:
                    await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass
            try:
                await interaction.response.edit_message(content="✅ Color set.", view=None)
            except discord.HTTPException:
                pass
            return
        # Modal-submit path: the modal interaction edits the *parent message* of
        # the component that opened it, which is the builder message.
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.HTTPException:
            pass

    # ----- buttons -----

    @discord.ui.button(label="Title", style=discord.ButtonStyle.secondary, row=0)
    async def b_title(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_TitleModal(self))

    @discord.ui.button(label="Description", style=discord.ButtonStyle.secondary, row=0)
    async def b_description(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_DescriptionModal(self))

    @discord.ui.button(label="Color", style=discord.ButtonStyle.secondary, row=0)
    async def b_color(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Pick a preset, or **Custom Hex**:", view=_ColorPickerView(self), ephemeral=True,
        )

    @discord.ui.button(label="Author", style=discord.ButtonStyle.secondary, row=0)
    async def b_author(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_AuthorModal(self))

    @discord.ui.button(label="Footer", style=discord.ButtonStyle.secondary, row=0)
    async def b_footer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_FooterModal(self))

    @discord.ui.button(label="Image", style=discord.ButtonStyle.secondary, row=1)
    async def b_image(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_ImageModal(self, kind="image"))

    @discord.ui.button(label="Thumbnail", style=discord.ButtonStyle.secondary, row=1)
    async def b_thumb(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_ImageModal(self, kind="thumbnail"))

    @discord.ui.button(label="Add Field", style=discord.ButtonStyle.secondary, row=1)
    async def b_addfield(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_AddFieldModal(self))

    @discord.ui.button(label="Clear Fields", style=discord.ButtonStyle.secondary, row=1)
    async def b_clearfields(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.state.fields.clear()
        await interaction.response.edit_message(embed=self.state.to_preview_embed(), view=self)

    @discord.ui.button(label="🔘 Buttons", style=discord.ButtonStyle.primary, row=2)
    async def b_buttons(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.bot is None:
            return await interaction.response.send_message(
                "❌ Button management isn't wired into this builder.", ephemeral=True,
            )
        # Need a saved name before we can attach buttons (foreign-key style).
        if not self._saved:
            if self.on_save is None or not self.name:
                return await interaction.response.send_message(
                    "❌ Save the embed first (click ✅ Save).", ephemeral=True,
                )
            script = self.state.to_script()
            if not script:
                return await interaction.response.send_message(
                    "❌ Embed is empty — add some content first.", ephemeral=True,
                )
            await self.bot.db.execute(
                """INSERT INTO saved_embeds (guild_id, name, script) VALUES ($1, $2, $3)
                   ON CONFLICT (guild_id, name) DO UPDATE SET script = EXCLUDED.script""",
                interaction.guild_id, self.name, script,
            )
            self._saved = True
        await _open_button_manager(interaction, self.bot, self.name)

    @discord.ui.button(label="👁 Preview", style=discord.ButtonStyle.primary, row=2)
    async def b_preview(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            content="**Preview** (only you see this):",
            embed=self.state.to_preview_embed(),
            ephemeral=True,
        )

    @discord.ui.button(label="✅ Save", style=discord.ButtonStyle.success, row=2)
    async def b_save(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_SaveModal(self))

    @discord.ui.button(label="🗑 Discard", style=discord.ButtonStyle.danger, row=2)
    async def b_discard(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(content="🗑 Builder discarded.", embed=None, view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            if self.message:
                await self.message.edit(content="⏲ Builder timed out.", view=self)
        except (discord.NotFound, discord.HTTPException):
            pass


# ============================================================
# Button Manager — guided UI for attaching buttons to a saved embed
# ============================================================

async def _open_button_manager(interaction: discord.Interaction, bot, embed_name: str):
    """Send the button-manager embed as an ephemeral with the manager view."""
    view = _ButtonManagerView(bot, embed_name, interaction.user.id)
    await view.refresh_state()
    embed = view.build_embed()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    view.message = await interaction.original_response()


async def _next_button_pos(bot, guild_id: int, embed_name: str) -> int:
    return await bot.db.fetchval(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM embed_buttons "
        "WHERE guild_id=$1 AND embed_name=$2",
        guild_id, embed_name,
    )


async def _persist_button(bot, guild_id: int, embed_name: str, *,
                          style: str, color: str, label: str,
                          target: Optional[str], emoji: Optional[str]):
    pos = await _next_button_pos(bot, guild_id, embed_name)
    await bot.db.execute(
        "INSERT INTO embed_buttons (guild_id, embed_name, position, style, color, label, target, emoji) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        guild_id, embed_name, pos, style, color, label, target, emoji,
    )


class _ButtonManagerView(discord.ui.View):
    def __init__(self, bot, embed_name: str, author_id: int):
        super().__init__(timeout=600)
        self.bot = bot
        self.embed_name = embed_name
        self.author_id = author_id
        self.message: Optional[discord.Message] = None
        self.current: list[dict] = []

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Not yours.", ephemeral=True)
            return False
        return True

    async def refresh_state(self):
        rows = await self.bot.db.fetch(
            "SELECT position, style, label, target, emoji FROM embed_buttons "
            "WHERE guild_id=$1 AND embed_name=$2 ORDER BY position",
            None, self.embed_name,  # guild_id filled below; see refresh_for
        )
        # We don't have guild_id in __init__; use a guild_id-injected refresh from the open helper.
        # Instead, do this: fetch all rows for this embed name across guilds is wrong.
        # We rely on refresh_for() to set self.current using a known guild_id.

    async def refresh_for(self, guild_id: int):
        rows = await self.bot.db.fetch(
            "SELECT position, style, label, target, emoji FROM embed_buttons "
            "WHERE guild_id=$1 AND embed_name=$2 ORDER BY position",
            guild_id, self.embed_name,
        )
        self.current = [dict(r) for r in rows]

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"🔘 Buttons — `{self.embed_name}`",
            description="Add link / role / open / form / ticket / verify buttons. They'll be attached "
                        "to this embed wherever it's sent.",
            color=discord.Color(0xFFFFFF),
        )
        if not self.current:
            embed.add_field(name="Current buttons", value="_none yet_", inline=False)
        else:
            lines = [
                f"`{b['position']+1}` · **{b['style']}** · `{b['label']}`"
                + (f" → `{b['target']}`" if b["target"] else "")
                + (f"  {b['emoji']}" if b["emoji"] else "")
                for b in self.current
            ]
            embed.add_field(name=f"Current buttons ({len(self.current)})", value="\n".join(lines), inline=False)
        embed.set_footer(text="Use ➕ buttons below to add. Discord allows up to 25 buttons total.")
        return embed

    async def _refresh_message(self, interaction: discord.Interaction):
        await self.refresh_for(interaction.guild_id)
        try:
            await interaction.edit_original_response(embed=self.build_embed(), view=self)
        except discord.HTTPException:
            try:
                await self.message.edit(embed=self.build_embed(), view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="➕ Link", style=discord.ButtonStyle.secondary, row=0)
    async def add_link(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_AddLinkModal(self))

    @discord.ui.button(label="➕ Role", style=discord.ButtonStyle.secondary, row=0)
    async def add_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_AddRoleModal(self))

    @discord.ui.button(label="➕ Open", style=discord.ButtonStyle.secondary, row=0)
    async def add_open(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_AddOpenModal(self))

    @discord.ui.button(label="➕ Form", style=discord.ButtonStyle.secondary, row=1)
    async def add_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_AddFormModal(self))

    @discord.ui.button(label="➕ Ticket", style=discord.ButtonStyle.secondary, row=1)
    async def add_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_AddTicketModal(self))

    @discord.ui.button(label="➕ Verify", style=discord.ButtonStyle.secondary, row=1)
    async def add_verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_AddVerifyModal(self))

    @discord.ui.button(label="🗑 Remove…", style=discord.ButtonStyle.danger, row=2)
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.refresh_for(interaction.guild_id)
        if not self.current:
            return await interaction.response.send_message("ℹ️ No buttons to remove.", ephemeral=True)
        options = [
            discord.SelectOption(
                label=f"#{b['position']+1} · {b['style']} · {b['label'][:40]}",
                value=str(b["position"]),
            )
            for b in self.current[:25]
        ]
        select = discord.ui.Select(placeholder="Pick a button to remove…", options=options)

        async def _cb(i: discord.Interaction):
            pos = int(i.data["values"][0])
            await self.bot.db.execute(
                "DELETE FROM embed_buttons WHERE guild_id=$1 AND embed_name=$2 AND position=$3",
                i.guild_id, self.embed_name, pos,
            )
            await self.refresh_for(i.guild_id)
            try:
                await self.message.edit(embed=self.build_embed(), view=self)
            except discord.HTTPException:
                pass
            await i.response.send_message("✅ Removed.", ephemeral=True)

        select.callback = _cb
        v = discord.ui.View(timeout=60)
        v.add_item(select)
        await interaction.response.send_message(view=v, ephemeral=True)

    @discord.ui.button(label="✖ Done", style=discord.ButtonStyle.success, row=2)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        for c in self.children:
            c.disabled = True
        self.stop()
        await interaction.response.edit_message(content="✅ Done. Buttons saved.", embed=None, view=self)


COLOR_NAMES = ("blurple", "green", "grey", "red")


def _norm_color(s: Optional[str]) -> str:
    s = (s or "").strip().lower()
    if s in ("gray",):
        return "grey"
    return s if s in COLOR_NAMES else "blurple"


class _AddLinkModal(discord.ui.Modal, title="Add Link Button"):
    label_field = discord.ui.TextInput(label="Label", max_length=80)
    url_field = discord.ui.TextInput(label="URL")
    emoji_field = discord.ui.TextInput(label="Emoji (optional)", required=False)

    def __init__(self, mgr: _ButtonManagerView):
        super().__init__()
        self.mgr = mgr

    async def on_submit(self, interaction: discord.Interaction):
        await _persist_button(
            self.mgr.bot, interaction.guild_id, self.mgr.embed_name,
            style="link", color="blurple", label=self.label_field.value,
            target=self.url_field.value, emoji=(self.emoji_field.value or None),
        )
        await self.mgr._refresh_message(interaction)


class _AddRoleModal(discord.ui.Modal, title="Add Role-Toggle Button"):
    label_field = discord.ui.TextInput(label="Label", max_length=80)
    role_field = discord.ui.TextInput(label="Role ID or @mention")
    color_field = discord.ui.TextInput(label="Color (blurple/green/grey/red)", required=False, max_length=10, default="blurple")
    emoji_field = discord.ui.TextInput(label="Emoji (optional)", required=False)

    def __init__(self, mgr: _ButtonManagerView):
        super().__init__()
        self.mgr = mgr

    async def on_submit(self, interaction: discord.Interaction):
        try:
            rid = int(self.role_field.value.strip().strip("<@&>"))
        except ValueError:
            return await interaction.response.send_message("❌ Invalid role.", ephemeral=True)
        if interaction.guild.get_role(rid) is None:
            return await interaction.response.send_message("❌ Role not found.", ephemeral=True)
        await _persist_button(
            self.mgr.bot, interaction.guild_id, self.mgr.embed_name,
            style="role", color=_norm_color(self.color_field.value),
            label=self.label_field.value, target=str(rid),
            emoji=(self.emoji_field.value or None),
        )
        await self.mgr._refresh_message(interaction)


class _AddOpenModal(discord.ui.Modal, title="Add Open-Embed Button"):
    label_field = discord.ui.TextInput(label="Label", max_length=80)
    target_field = discord.ui.TextInput(label="Other saved-embed name")
    color_field = discord.ui.TextInput(label="Color", required=False, default="grey", max_length=10)
    emoji_field = discord.ui.TextInput(label="Emoji (optional)", required=False)

    def __init__(self, mgr: _ButtonManagerView):
        super().__init__()
        self.mgr = mgr

    async def on_submit(self, interaction: discord.Interaction):
        target = self.target_field.value.strip().lower()
        exists = await self.mgr.bot.db.fetchval(
            "SELECT 1 FROM saved_embeds WHERE guild_id=$1 AND name=$2",
            interaction.guild_id, target,
        )
        if not exists:
            return await interaction.response.send_message(f"❌ No saved embed `{target}`.", ephemeral=True)
        await _persist_button(
            self.mgr.bot, interaction.guild_id, self.mgr.embed_name,
            style="open", color=_norm_color(self.color_field.value),
            label=self.label_field.value, target=target,
            emoji=(self.emoji_field.value or None),
        )
        await self.mgr._refresh_message(interaction)


class _AddFormModal(discord.ui.Modal, title="Add Form Button"):
    label_field = discord.ui.TextInput(label="Label", max_length=80)
    form_field = discord.ui.TextInput(label="Form name")
    color_field = discord.ui.TextInput(label="Color", required=False, default="blurple", max_length=10)
    emoji_field = discord.ui.TextInput(label="Emoji (optional)", required=False)

    def __init__(self, mgr: _ButtonManagerView):
        super().__init__()
        self.mgr = mgr

    async def on_submit(self, interaction: discord.Interaction):
        form_name = self.form_field.value.strip().lower()
        exists = await self.mgr.bot.db.fetchval(
            "SELECT 1 FROM forms WHERE guild_id=$1 AND name=$2",
            interaction.guild_id, form_name,
        )
        if not exists:
            return await interaction.response.send_message(f"❌ No form `{form_name}`.", ephemeral=True)
        await _persist_button(
            self.mgr.bot, interaction.guild_id, self.mgr.embed_name,
            style="form", color=_norm_color(self.color_field.value),
            label=self.label_field.value, target=form_name,
            emoji=(self.emoji_field.value or None),
        )
        await self.mgr._refresh_message(interaction)


class _AddTicketModal(discord.ui.Modal, title="Add Ticket Button"):
    label_field = discord.ui.TextInput(label="Label", max_length=80)
    panel_field = discord.ui.TextInput(label="Ticket panel name")
    color_field = discord.ui.TextInput(label="Color", required=False, default="blurple", max_length=10)
    emoji_field = discord.ui.TextInput(label="Emoji (optional)", required=False)

    def __init__(self, mgr: _ButtonManagerView):
        super().__init__()
        self.mgr = mgr

    async def on_submit(self, interaction: discord.Interaction):
        panel_name = self.panel_field.value.strip().lower()
        exists = await self.mgr.bot.db.fetchval(
            "SELECT 1 FROM ticket_panels WHERE guild_id=$1 AND name=$2",
            interaction.guild_id, panel_name,
        )
        if not exists:
            return await interaction.response.send_message(f"❌ No ticket panel `{panel_name}`.", ephemeral=True)
        await _persist_button(
            self.mgr.bot, interaction.guild_id, self.mgr.embed_name,
            style="ticket", color=_norm_color(self.color_field.value),
            label=self.label_field.value, target=panel_name,
            emoji=(self.emoji_field.value or None),
        )
        await self.mgr._refresh_message(interaction)


class _AddVerifyModal(discord.ui.Modal, title="Add Verify Button"):
    label_field = discord.ui.TextInput(label="Label", required=False, default="Verify", max_length=80)
    color_field = discord.ui.TextInput(label="Color", required=False, default="green", max_length=10)
    emoji_field = discord.ui.TextInput(label="Emoji (optional)", required=False, default="✅")

    def __init__(self, mgr: _ButtonManagerView):
        super().__init__()
        self.mgr = mgr

    async def on_submit(self, interaction: discord.Interaction):
        await _persist_button(
            self.mgr.bot, interaction.guild_id, self.mgr.embed_name,
            style="verify", color=_norm_color(self.color_field.value),
            label=self.label_field.value or "Verify", target=str(interaction.guild_id),
            emoji=(self.emoji_field.value or None),
        )
        await self.mgr._refresh_message(interaction)
