"""Sentinel bot entrypoint."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from utils import database
from utils.config import GuildConfig

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DEFAULT_PREFIX = os.getenv("DEFAULT_PREFIX", ".")
OWNER_ID = int(os.getenv("OWNER_ID", "0")) or None

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set in the environment.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("sentinel")

# Allowlist for slash commands. Everything else stays accessible via prefix only,
# so the slash menu mirrors Bleed's vibe: short, basic, user-facing actions.
SLASH_ALLOWLIST = frozenset({
    # General
    "help", "afk", "snipe", "editsnipe", "crypto",
    # Moderation basics (already hybrid)
    "ban", "kick", "unban", "mute", "unmute", "warn",
    "purge", "clear", "slowmode", "lock", "unlock", "nuke",
    # Ticket lifecycle (Bleed parity)
    "add", "claim", "unclaim", "close", "transcript",
})

INITIAL_COGS = (
    "cogs.configure",
    "cogs.restrictions",
    "cogs.moderation",
    "cogs.role_manager",
    "cogs.channel_manager",
    "cogs.utility",
    "cogs.afk",
    "cogs.snipe",
    "cogs.guildlock",
    "cogs.vanity",
    "cogs.admin",
    "cogs.invites",        # must load before system_messages and greet
    "cogs.system_messages",
    "cogs.greet",
    "cogs.booster",
    "cogs.embeds",
    "cogs.forms",
    "cogs.custom_commands",
    "cogs.sticky",
    "cogs.tickets",
    "cogs.verify",
    "cogs.customize",
    "cogs.logging",
    "cogs.counters",
    "cogs.autoresponder",
    "cogs.crypto",
    "cogs.stocks",
    "cogs.roblox",
    "cogs.help",
)


async def _resolve_prefix(bot: "Sentinel", message: discord.Message):
    """Per-guild prefix resolver. Always allows @mentioning the bot."""
    base = bot.guild_config.get_prefix(message.guild.id if message.guild else None)
    return commands.when_mentioned_or(base)(bot, message)


class Sentinel(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True
        intents.presences = True  # required for the vanity/server-tag detector

        super().__init__(
            command_prefix=_resolve_prefix,
            intents=intents,
            help_command=None,
            owner_id=OWNER_ID,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False),
        )

        self.start_time = datetime.now(timezone.utc)
        self.db = database.from_env()
        self.guild_config = GuildConfig(self.db, DEFAULT_PREFIX)
        self.config = self._load_static_config()

    @staticmethod
    def _load_static_config() -> dict:
        defaults = {"version": "0.1.0", "support_server": None}
        path = Path("config.json")
        if not path.exists():
            return defaults
        try:
            text = path.read_text(encoding="utf-8").strip()
            if not text:
                return defaults
            data = json.loads(text)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("config.json unreadable (%s); using defaults", e)
            return defaults
        return {**defaults, **data}

    async def setup_hook(self) -> None:
        async def _on_tree_error(interaction: discord.Interaction, error: Exception):
            from discord import app_commands
            if isinstance(error, app_commands.CommandOnCooldown):
                return await interaction.response.send_message(
                    f"On cooldown — try again in {error.retry_after:.1f}s.",
                    ephemeral=True,
                )
            log.exception("Unhandled slash error", exc_info=error)

        self.tree.on_error = _on_tree_error

        # Connect DB before loading cogs so cogs can register their own tables.
        await self.db.connect()
        await self.guild_config.load()
        log.info("Database connected; %d cached prefix overrides", len(self.guild_config._prefix_cache))

        for cog in INITIAL_COGS:
            try:
                await self.load_extension(cog)
                log.info("Loaded %s", cog)
            except Exception:
                log.exception("Failed to load %s", cog)

        # Defensive cleanup: only the allowlisted commands are exposed via slash.
        # Anything that slipped in (e.g. a hybrid command somewhere) gets pruned
        # so the slash menu stays clean (Bleed-style: simple actionable commands only).
        for cmd in list(self.tree.get_commands()):
            if cmd.name not in SLASH_ALLOWLIST:
                self.tree.remove_command(cmd.name, type=cmd.type)
                log.info("Pruned %s from slash tree (not on allowlist)", cmd.name)

        # Sync application (slash) commands globally on boot.
        # Discord can take up to ~1 hour to propagate global syncs the first time.
        try:
            synced = await self.tree.sync()
            log.info("Synced %d application commands", len(synced))
        except discord.HTTPException:
            log.exception("Slash command sync failed")

    async def on_ready(self):
        log.info("Logged in as %s (%s) | %d guilds", self.user, self.user.id, len(self.guilds))
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"{len(self.guilds)} servers | {DEFAULT_PREFIX}help",
            )
        )

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.DisabledCommand):
            return await ctx.send("❌ That command is disabled in this server.")
        if isinstance(error, commands.CheckFailure):
            return await ctx.send(f"❌ {error}")
        if isinstance(error, commands.MissingPermissions):
            return await ctx.send("❌ You don't have permission to use this command.")
        if isinstance(error, commands.BotMissingPermissions):
            missing = ", ".join(error.missing_permissions)
            return await ctx.send(f"❌ I'm missing required permissions: `{missing}`")
        if isinstance(error, commands.MissingRequiredArgument):
            prefix = self.guild_config.get_prefix(ctx.guild.id if ctx.guild else None)
            return await ctx.send(
                f"❌ Missing argument: `{error.param.name}`\nUse `{prefix}help {ctx.command}` for details."
            )
        if isinstance(error, commands.BadArgument):
            return await ctx.send(f"❌ Invalid argument: {error}")
        if isinstance(error, commands.CommandOnCooldown):
            return await ctx.send(f"⏰ On cooldown — try again in {error.retry_after:.1f}s.")
        if isinstance(error, commands.NoPrivateMessage):
            return await ctx.send("❌ This command can't be used in DMs.")

        log.exception("Unhandled command error in %s", ctx.command, exc_info=error)
        await ctx.send("❌ An unexpected error occurred. The error has been logged.")

    async def close(self):
        await self.db.close()
        await super().close()


bot = Sentinel()


@bot.command(hidden=True)
@commands.is_owner()
async def reload(ctx, cog: str):
    """Reload a cog (owner only)."""
    try:
        await bot.reload_extension(f"cogs.{cog}")
        await ctx.send(f"✅ Reloaded `{cog}`")
    except Exception as e:
        await ctx.send(f"❌ Failed to reload: {e}")


@bot.command(hidden=True)
@commands.is_owner()
async def sync(ctx):
    """Force-resync application commands (owner only)."""
    synced = await bot.tree.sync()
    await ctx.send(f"✅ Synced {len(synced)} commands")


@bot.command(hidden=True)
@commands.is_owner()
async def shutdown(ctx):
    """Shutdown the bot (owner only)."""
    await ctx.send("👋 Shutting down...")
    await bot.close()


if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)
