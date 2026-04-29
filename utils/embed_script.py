"""Bleed-style embed script parser + variable substitution.

Syntax (Bleed parity):
  - Parameters separated by `$v`:           {title: hi}$v{description: hello}
  - Plain message text:                     {message: plain text outside embed}
  - Simple keys:                            title, description, url, color,
                                            image, thumbnail, timestamp
  - Compound keys (split on `&&`):          author: name && icon && url
                                            field: name && value [&& inline]
                                            footer: text && icon
                                            button: type && label && url [&& enabled|disabled]

Variables (subset of Bleed's set):
  {user}, {user.mention}, {user.name}, {user.id}, {user.display_name},
  {user.avatar}, {user.tag}, {user.created_at}, {user.joined_at},
  {guild.name}, {guild.id}, {guild.count}, {guild.icon}, {guild.owner_id},
  {channel.name}, {channel.id}, {channel.mention}

Returns (content_str, embed_or_None, view_or_None).
"""
from __future__ import annotations

import re
from typing import Optional

import discord

# `$v` is the parameter separator. We split on it but only at the top level —
# inside a {key: value} block braces aren't nested in this DSL so a plain split is safe.
PARAM_SPLIT = re.compile(r"\$v")
# A param block looks like {key: rest...} where rest may contain colons.
PARAM_BLOCK = re.compile(r"^\s*\{\s*(\w+)\s*:\s*(.*)\}\s*$", re.DOTALL)
SIMPLE_BLOCK = re.compile(r"^\s*\{\s*(\w+)\s*\}\s*$")  # for {timestamp}

BUTTON_STYLES = {
    "link": discord.ButtonStyle.link,
    "blurple": discord.ButtonStyle.primary,
    "green": discord.ButtonStyle.success,
    "grey": discord.ButtonStyle.secondary,
    "gray": discord.ButtonStyle.secondary,
    "red": discord.ButtonStyle.danger,
}


def _color(value: str) -> Optional[discord.Color]:
    v = value.strip().lstrip("#")
    if not v:
        return None
    try:
        return discord.Color(int(v, 16))
    except ValueError:
        return None


def substitute_variables(text: str, *, user: Optional[discord.abc.User] = None,
                         guild: Optional[discord.Guild] = None,
                         channel: Optional[discord.abc.GuildChannel] = None) -> str:
    if not text:
        return text
    repl: dict[str, str] = {}
    if user is not None:
        repl.update({
            "{user}": user.mention,
            "{user.mention}": user.mention,
            "{user.name}": user.name,
            "{user.id}": str(user.id),
            "{user.display_name}": getattr(user, "display_name", user.name),
            "{user.tag}": str(user),
            "{user.avatar}": user.display_avatar.url,
            "{user.created_at}": f"<t:{int(user.created_at.timestamp())}:R>",
        })
        if isinstance(user, discord.Member) and user.joined_at:
            repl["{user.joined_at}"] = f"<t:{int(user.joined_at.timestamp())}:R>"
    if guild is not None:
        repl.update({
            "{guild.name}": guild.name,
            "{guild.id}": str(guild.id),
            "{guild.count}": str(guild.member_count or 0),
            "{guild.icon}": guild.icon.url if guild.icon else "",
            "{guild.owner_id}": str(guild.owner_id or ""),
        })
    if channel is not None:
        repl.update({
            "{channel.name}": getattr(channel, "name", ""),
            "{channel.id}": str(channel.id),
            "{channel.mention}": getattr(channel, "mention", ""),
        })
    for k, v in repl.items():
        text = text.replace(k, v)
    return text


class ParsedScript:
    def __init__(self):
        self.content: Optional[str] = None
        self.embed: Optional[discord.Embed] = None
        self.view: Optional[discord.ui.View] = None

    @property
    def is_empty(self) -> bool:
        return self.content is None and self.embed is None


def parse(script: str) -> ParsedScript:
    """Parse the embed-script DSL into (content, embed, view).

    If `script` contains no `{...}` blocks at all, it's treated as plain text.
    """
    result = ParsedScript()
    if not script:
        return result

    # Plain-text fast path: no DSL braces at all.
    if "{" not in script:
        result.content = script
        return result

    embed = discord.Embed()
    embed_used = False
    view = discord.ui.View(timeout=None)
    button_count = 0

    parts = PARAM_SPLIT.split(script)
    for raw in parts:
        raw = raw.strip()
        if not raw:
            continue

        m_simple = SIMPLE_BLOCK.match(raw)
        if m_simple and m_simple.group(1).lower() == "timestamp":
            embed.timestamp = discord.utils.utcnow()
            embed_used = True
            continue

        m = PARAM_BLOCK.match(raw)
        if not m:
            # Loose text outside any {} — append to content.
            result.content = (result.content or "") + raw
            continue

        key = m.group(1).lower()
        value = m.group(2).strip()

        if key in ("content", "message"):
            result.content = (result.content or "") + value
        elif key == "title":
            embed.title = value[:256]
            embed_used = True
        elif key == "description":
            embed.description = value[:4096]
            embed_used = True
        elif key == "url":
            embed.url = value
            embed_used = True
        elif key == "color":
            c = _color(value)
            if c is not None:
                embed.color = c
                embed_used = True
        elif key == "image":
            embed.set_image(url=value)
            embed_used = True
        elif key == "thumbnail":
            embed.set_thumbnail(url=value)
            embed_used = True
        elif key == "author":
            parts2 = [p.strip() for p in value.split("&&")]
            kwargs = {"name": parts2[0][:256]}
            if len(parts2) > 1 and parts2[1]:
                kwargs["icon_url"] = parts2[1]
            if len(parts2) > 2 and parts2[2]:
                kwargs["url"] = parts2[2]
            embed.set_author(**kwargs)
            embed_used = True
        elif key == "footer":
            parts2 = [p.strip() for p in value.split("&&")]
            kwargs = {"text": parts2[0][:2048]}
            if len(parts2) > 1 and parts2[1]:
                kwargs["icon_url"] = parts2[1]
            embed.set_footer(**kwargs)
            embed_used = True
        elif key == "field":
            parts2 = [p.strip() for p in value.split("&&")]
            if len(parts2) >= 2:
                inline = len(parts2) >= 3 and parts2[2].lower() == "inline"
                embed.add_field(name=parts2[0][:256], value=parts2[1][:1024], inline=inline)
                embed_used = True
        elif key == "button":
            parts2 = [p.strip() for p in value.split("&&")]
            if len(parts2) >= 2 and button_count < 25:
                style_name = parts2[0].lower()
                style = BUTTON_STYLES.get(style_name, discord.ButtonStyle.secondary)
                label = parts2[1][:80]
                url = parts2[2] if len(parts2) > 2 and parts2[2] else None
                disabled = len(parts2) > 3 and parts2[3].lower() == "disabled"
                if style == discord.ButtonStyle.link and url:
                    view.add_item(discord.ui.Button(style=style, label=label, url=url, disabled=disabled))
                else:
                    # Non-link buttons need a custom_id; bare buttons here are a no-op
                    # placeholder. Role/embed-action buttons are wired up elsewhere.
                    view.add_item(discord.ui.Button(
                        style=style, label=label, disabled=True,
                        custom_id=f"static_button_{button_count}",
                    ))
                button_count += 1

    if embed_used:
        result.embed = embed
    if view.children:
        result.view = view
    return result


def render(script: str, **vars) -> ParsedScript:
    """Substitute variables, then parse."""
    return parse(substitute_variables(script, **vars))
