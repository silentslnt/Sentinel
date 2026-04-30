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
            for child in self._view.children:
                child.disabled = True
            self._view.stop()
            try:
                await self._view.message.edit(
                    content=f"✅ Saved as `{name}`.",
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
        timeout: float = 900.0,
    ):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.name = name
        self.on_save = on_save
        self.state = EmbedBuilderState()
        self.message: Optional[discord.Message] = None

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

    @discord.ui.button(label="👁 Preview Public", style=discord.ButtonStyle.primary, row=2)
    async def b_preview(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Posts the preview as if it were live (ephemeral so it doesn't leak).
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
