"""Verification system.

Configured per-guild:
  - access_role:    granted on success
  - threshold:      minimum total approximate member count across submitted invites
  - log_channel:    where staff sees outcomes
  - panel_embed:    saved embed used as the public Verify panel (optional)

Flow when a user clicks a `verify` embed button:
  1. Bot opens a private verification ticket (uses the `verify` ticket panel
     if configured; falls back to a temporary channel under the access category).
  2. User posts their invite links in the ticket. The bot listens for messages
     in verification tickets and parses invites automatically.
  3. Bot resolves each invite via Discord's API (`/invites/<code>?with_counts=true`),
     skips servers already used by previous verifications (verify_used_invites table),
     sums approximate_member_count.
  4. If total >= threshold → grants access_role + closes ticket. Otherwise the
     ticket stays open with a "Give Access / Reject" action set for staff.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import aiohttp
import discord
from discord.ext import commands

from utils.checks import is_guild_admin

log = logging.getLogger("sentinel.verify")

SCHEMA = """
CREATE TABLE IF NOT EXISTS verify_config (
    guild_id      BIGINT PRIMARY KEY,
    access_role_id BIGINT,
    threshold     INTEGER NOT NULL DEFAULT 1000,
    log_channel_id BIGINT,
    category_id   BIGINT,
    staff_role_id BIGINT
);

CREATE TABLE IF NOT EXISTS verify_used_invites (
    guild_id        BIGINT NOT NULL,
    source_guild_id BIGINT NOT NULL,
    invite_code     TEXT NOT NULL,
    used_by_user_id BIGINT NOT NULL,
    used_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, source_guild_id)
);

CREATE TABLE IF NOT EXISTS verify_tickets (
    channel_id   BIGINT PRIMARY KEY,
    guild_id     BIGINT NOT NULL,
    user_id      BIGINT NOT NULL,
    opened_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/([A-Za-z0-9-]+)",
    re.IGNORECASE,
)


# ---------------- Verify panel button ----------------

class StartVerifyButton(discord.ui.DynamicItem[discord.ui.Button],
                        template=r"sentinel:verifystart:(?P<guild_id>\d+)"):
    def __init__(self, guild_id: int):
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.success,
                label="Verify",
                emoji="✅",
                custom_id=f"sentinel:verifystart:{guild_id}",
            )
        )
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction):
        cog: "Verify" = interaction.client.get_cog("Verify")  # type: ignore
        if cog is None:
            return await interaction.response.send_message("❌ Verify system unavailable.", ephemeral=True)
        await cog.start_verification(interaction)


# ---------------- In-ticket staff buttons ----------------

class _StaffActions(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Give Access", style=discord.ButtonStyle.success, emoji="✅",
                       custom_id="sentinel:verifystaff:give")
    async def give(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: "Verify" = interaction.client.get_cog("Verify")  # type: ignore
        if cog is None:
            return
        await cog.staff_give_access(interaction)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, emoji="✖",
                       custom_id="sentinel:verifystaff:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: "Verify" = interaction.client.get_cog("Verify")  # type: ignore
        if cog is None:
            return
        await cog.staff_reject(interaction)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, emoji="🔒",
                       custom_id="sentinel:verifystaff:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: "Verify" = interaction.client.get_cog("Verify")  # type: ignore
        if cog is None:
            return
        await cog.staff_close(interaction)


# ---------------- Cog ----------------

class Verify(commands.Cog):
    """🔐 Verification system"""

    def __init__(self, bot):
        self.bot = bot
        self._http: Optional[aiohttp.ClientSession] = None

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)
        self.bot.add_dynamic_items(StartVerifyButton)
        self.bot.add_view(_StaffActions())
        self._http = aiohttp.ClientSession()

    async def cog_unload(self):
        if self._http is not None:
            await self._http.close()

    # ---------- helpers ----------

    async def _config(self, guild_id: int) -> Optional[dict]:
        row = await self.bot.db.fetchrow("SELECT * FROM verify_config WHERE guild_id=$1", guild_id)
        return dict(row) if row else None

    async def _upsert(self, guild_id: int, **fields):
        await self.bot.db.execute(
            "INSERT INTO verify_config (guild_id) VALUES ($1) ON CONFLICT DO NOTHING", guild_id,
        )
        if fields:
            sets = ", ".join(f"{k}=${i+2}" for i, k in enumerate(fields))
            await self.bot.db.execute(
                f"UPDATE verify_config SET {sets} WHERE guild_id=$1",
                guild_id, *fields.values(),
            )

    async def _resolve_invite(self, code: str) -> Optional[dict]:
        """Returns {guild_id, member_count} or None."""
        if self._http is None:
            return None
        try:
            async with self._http.get(
                f"https://discord.com/api/v10/invites/{code}?with_counts=true",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                guild = data.get("guild") or {}
                return {
                    "guild_id": int(guild.get("id", 0)) if guild.get("id") else None,
                    "guild_name": guild.get("name") or "?",
                    "member_count": data.get("approximate_member_count") or 0,
                }
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None

    # ---------- start a verification ----------

    async def start_verification(self, interaction: discord.Interaction):
        cfg = await self._config(interaction.guild_id)
        if cfg is None or cfg.get("category_id") is None or cfg.get("access_role_id") is None:
            return await interaction.response.send_message(
                "❌ Verification isn't fully configured. Ask a staff member.", ephemeral=True,
            )
        # One open verification ticket per user.
        existing = await self.bot.db.fetchrow(
            "SELECT channel_id FROM verify_tickets WHERE guild_id=$1 AND user_id=$2",
            interaction.guild_id, interaction.user.id,
        )
        if existing:
            ch = interaction.guild.get_channel(existing["channel_id"])
            if ch:
                return await interaction.response.send_message(
                    f"❌ You already have a verification open: {ch.mention}", ephemeral=True,
                )
            await self.bot.db.execute(
                "DELETE FROM verify_tickets WHERE channel_id=$1", existing["channel_id"],
            )

        category = interaction.guild.get_channel(cfg["category_id"])
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message("❌ Category missing.", ephemeral=True)

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, attach_files=True,
                embed_links=True, read_message_history=True,
            ),
            interaction.guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True,
                manage_messages=True, embed_links=True,
            ),
        }
        if cfg.get("staff_role_id"):
            staff_role = interaction.guild.get_role(cfg["staff_role_id"])
            if staff_role:
                overwrites[staff_role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, manage_messages=True,
                    attach_files=True, embed_links=True,
                )

        try:
            channel = await category.create_text_channel(
                name=f"verify-{interaction.user.name}".lower()[:90],
                overwrites=overwrites,
                reason=f"Verification opened by {interaction.user}",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            return await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)

        await self.bot.db.execute(
            "INSERT INTO verify_tickets (channel_id, guild_id, user_id) VALUES ($1, $2, $3)",
            channel.id, interaction.guild_id, interaction.user.id,
        )

        threshold = cfg["threshold"]
        intro = discord.Embed(
            title="✅ VERIFICATION TICKET",
            description=(
                f"**Important**\n"
                f"Send **all of your server invites in one message** so the bot can total the members.\n"
                f"You need **{threshold:,}+ total members** from servers that were not already used "
                f"for another verification.\n\n"
                f"If the member total passes, staff can give access after reviewing the result and any "
                f"ownership proof requested.\n"
                f"Do not ping staff or owners. Wait patiently until a manager reviews your ticket."
            ),
            color=discord.Color.blurple(),
        )
        await channel.send(content=interaction.user.mention, embed=intro, view=_StaffActions())

        await interaction.response.send_message(f"✅ Verification opened: {channel.mention}", ephemeral=True)

    # ---------- listener: parse invites posted in verification channels ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        ticket = await self.bot.db.fetchrow(
            "SELECT * FROM verify_tickets WHERE channel_id=$1", message.channel.id,
        )
        if ticket is None or message.author.id != ticket["user_id"]:
            return
        codes = INVITE_RE.findall(message.content)
        if not codes:
            return

        cfg = await self._config(message.guild.id)
        if cfg is None:
            return

        await message.channel.send(f"🔎 Checking {len(codes)} invite(s)…")

        seen_source_guilds: set[int] = set()
        results: list[tuple[str, dict]] = []
        total = 0
        already_used: list[str] = []
        invalid: list[str] = []

        for code in codes:
            data = await self._resolve_invite(code)
            if data is None or not data.get("guild_id"):
                invalid.append(code)
                continue
            sgid = data["guild_id"]
            if sgid in seen_source_guilds:
                continue  # duplicate within this submission
            seen_source_guilds.add(sgid)

            used = await self.bot.db.fetchrow(
                "SELECT 1 FROM verify_used_invites WHERE guild_id=$1 AND source_guild_id=$2",
                message.guild.id, sgid,
            )
            if used:
                already_used.append(f"{data['guild_name']} ({sgid})")
                continue

            results.append((code, data))
            total += data["member_count"]

        threshold = cfg["threshold"]
        embed = discord.Embed(
            title="🔎 Invite Check Result",
            color=discord.Color.green() if total >= threshold else discord.Color.orange(),
        )
        if results:
            lines = [
                f"• **{d['guild_name']}** — {d['member_count']:,} members"
                for _, d in results
            ]
            embed.add_field(name="Counted", value="\n".join(lines)[:1024], inline=False)
        if already_used:
            embed.add_field(
                name="Already used (skipped)",
                value="\n".join(already_used)[:1024], inline=False,
            )
        if invalid:
            embed.add_field(name="Invalid / expired", value=", ".join(invalid)[:1024], inline=False)
        embed.add_field(
            name="Total / Threshold",
            value=f"**{total:,}** / {threshold:,}",
            inline=False,
        )
        await message.channel.send(embed=embed)

        if total >= threshold and results:
            # Auto-grant access.
            role = message.guild.get_role(cfg["access_role_id"]) if cfg["access_role_id"] else None
            if role and role < message.guild.me.top_role:
                try:
                    await message.author.add_roles(role, reason="Verification auto-pass")
                except discord.Forbidden:
                    await message.channel.send("❌ Couldn't assign role automatically. Staff will follow up.")
                else:
                    await message.channel.send(
                        f"🎉 Access granted — welcome, {message.author.mention}! Closing ticket shortly.",
                    )

            # Mark sources as used
            for _, d in results:
                await self.bot.db.execute(
                    """INSERT INTO verify_used_invites
                       (guild_id, source_guild_id, invite_code, used_by_user_id)
                       VALUES ($1, $2, $3, $4)
                       ON CONFLICT DO NOTHING""",
                    message.guild.id, d["guild_id"], "—", message.author.id,
                )

            await self._log_outcome(message.guild, message.author, "passed", total, threshold)
            await self._delete_verify_ticket(message.channel)

    # ---------- staff actions ----------

    async def staff_give_access(self, interaction: discord.Interaction):
        ticket = await self.bot.db.fetchrow(
            "SELECT * FROM verify_tickets WHERE channel_id=$1", interaction.channel_id,
        )
        if ticket is None:
            return await interaction.response.send_message("❌ Not a verify ticket.", ephemeral=True)
        cfg = await self._config(interaction.guild_id)
        if cfg is None:
            return await interaction.response.send_message("❌ Verify not configured.", ephemeral=True)
        if not self._is_staff(interaction, cfg):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)

        member = interaction.guild.get_member(ticket["user_id"])
        role = interaction.guild.get_role(cfg["access_role_id"]) if cfg["access_role_id"] else None
        if member is None or role is None:
            return await interaction.response.send_message("❌ User or role missing.", ephemeral=True)
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message("❌ Role above mine.", ephemeral=True)

        try:
            await member.add_roles(role, reason=f"Manual verify by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("❌ Can't assign role.", ephemeral=True)

        await interaction.response.send_message(f"✅ Access granted to {member.mention}. Closing…")
        await self._log_outcome(interaction.guild, member, "manual_pass", 0, 0, by=interaction.user)
        await self._delete_verify_ticket(interaction.channel)

    async def staff_reject(self, interaction: discord.Interaction):
        ticket = await self.bot.db.fetchrow(
            "SELECT * FROM verify_tickets WHERE channel_id=$1", interaction.channel_id,
        )
        if ticket is None:
            return await interaction.response.send_message("❌ Not a verify ticket.", ephemeral=True)
        cfg = await self._config(interaction.guild_id)
        if cfg is None or not self._is_staff(interaction, cfg):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        member = interaction.guild.get_member(ticket["user_id"])
        await interaction.response.send_message("❌ Verification rejected. Closing…")
        if member:
            await self._log_outcome(interaction.guild, member, "rejected", 0, 0, by=interaction.user)
        await self._delete_verify_ticket(interaction.channel)

    async def staff_close(self, interaction: discord.Interaction):
        ticket = await self.bot.db.fetchrow(
            "SELECT * FROM verify_tickets WHERE channel_id=$1", interaction.channel_id,
        )
        if ticket is None:
            return await interaction.response.send_message("❌ Not a verify ticket.", ephemeral=True)
        cfg = await self._config(interaction.guild_id)
        if cfg is None or not self._is_staff(interaction, cfg):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        await interaction.response.send_message("🔒 Closing…")
        await self._delete_verify_ticket(interaction.channel)

    @staticmethod
    def _is_staff(interaction: discord.Interaction, cfg: dict) -> bool:
        if interaction.user.guild_permissions.manage_guild:
            return True
        rid = cfg.get("staff_role_id")
        if rid is None:
            return False
        return any(r.id == rid for r in interaction.user.roles)

    async def _log_outcome(self, guild: discord.Guild, member: discord.abc.User, outcome: str,
                           total: int, threshold: int, *, by: Optional[discord.abc.User] = None):
        cfg = await self._config(guild.id)
        if cfg is None or cfg.get("log_channel_id") is None:
            return
        ch = guild.get_channel(cfg["log_channel_id"])
        if ch is None:
            return
        embed = discord.Embed(
            title=f"🔐 Verify outcome: {outcome}",
            color=discord.Color.green() if "pass" in outcome else discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=True)
        if total or threshold:
            embed.add_field(name="Total / Threshold", value=f"{total:,} / {threshold:,}", inline=True)
        if by is not None:
            embed.add_field(name="By", value=by.mention, inline=True)
        try:
            await ch.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _delete_verify_ticket(self, channel: discord.TextChannel):
        await self.bot.db.execute("DELETE FROM verify_tickets WHERE channel_id=$1", channel.id)
        try:
            await channel.delete(reason="Verification closed")
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ---------- prefix commands ----------

    @commands.group(name="verify", aliases=["vf"], invoke_without_command=True)
    @commands.guild_only()
    @commands.check(is_guild_admin)
    async def verify(self, ctx):
        """Verification configuration."""
        prefix = self.bot.guild_config.get_prefix(ctx.guild.id)
        await ctx.send(
            f"🔐 **Verification**\n"
            f"`{prefix}verify role <@role>` · role granted on success\n"
            f"`{prefix}verify threshold <number>` · min total member count\n"
            f"`{prefix}verify category <#category>` · where verify tickets live\n"
            f"`{prefix}verify staff <@role>` · staff role for verify tickets\n"
            f"`{prefix}verify log <#channel>` · log outcomes\n"
            f"`{prefix}verify panel <#channel>` · post the Verify button\n"
            f"`{prefix}verify config` · view current setup\n"
            f"`{prefix}verify resetinvite <guild_id>` · clear used-server flag",
        )

    @verify.command(name="role")
    async def v_role(self, ctx, role: discord.Role):
        if role >= ctx.guild.me.top_role:
            return await ctx.send("❌ Role above mine.")
        await self._upsert(ctx.guild.id, access_role_id=role.id)
        await ctx.send(f"✅ Access role set to {role.mention}.")

    @verify.command(name="threshold")
    async def v_threshold(self, ctx, n: int):
        if n < 1:
            return await ctx.send("❌ Threshold must be ≥ 1.")
        await self._upsert(ctx.guild.id, threshold=n)
        await ctx.send(f"✅ Threshold set to **{n:,}** members.")

    @verify.command(name="category")
    async def v_category(self, ctx, category: discord.CategoryChannel):
        await self._upsert(ctx.guild.id, category_id=category.id)
        await ctx.send(f"✅ Verify tickets will be created in {category.mention}.")

    @verify.command(name="staff")
    async def v_staff(self, ctx, role: discord.Role):
        await self._upsert(ctx.guild.id, staff_role_id=role.id)
        await ctx.send(f"✅ Staff role set to {role.mention}.")

    @verify.command(name="log")
    async def v_log(self, ctx, channel: discord.TextChannel):
        await self._upsert(ctx.guild.id, log_channel_id=channel.id)
        await ctx.send(f"✅ Log channel set to {channel.mention}.")

    @verify.command(name="panel")
    async def v_panel(self, ctx, channel: discord.TextChannel):
        cfg = await self._config(ctx.guild.id)
        if cfg is None or cfg.get("category_id") is None or cfg.get("access_role_id") is None:
            return await ctx.send("❌ Configure category and role first.")
        embed = discord.Embed(
            title="✅ VERIFICATION PROCESS",
            description=(
                "**Access Requirement**\n"
                "This server is reserved for server owners.\n"
                f"Send all your server invites in one message. Their combined member count must reach "
                f"**{cfg['threshold']:,}+ members.**\n"
                "Reused servers are detected automatically and do not count toward the total.\n"
                "Staff may request ownership proof before access is granted.\n"
                "Click **Verify** to start."
            ),
            color=discord.Color.blurple(),
        )
        view = discord.ui.View(timeout=None)
        view.add_item(StartVerifyButton(ctx.guild.id))
        try:
            await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            return await ctx.send(f"❌ Can't send in {channel.mention}.")
        await ctx.send(f"✅ Panel posted in {channel.mention}.")

    @verify.command(name="config")
    async def v_config(self, ctx):
        cfg = await self._config(ctx.guild.id)
        if cfg is None:
            return await ctx.send("ℹ️ Not configured.")
        role = ctx.guild.get_role(cfg["access_role_id"]) if cfg["access_role_id"] else None
        cat = ctx.guild.get_channel(cfg["category_id"]) if cfg["category_id"] else None
        staff = ctx.guild.get_role(cfg["staff_role_id"]) if cfg["staff_role_id"] else None
        ch = ctx.guild.get_channel(cfg["log_channel_id"]) if cfg["log_channel_id"] else None
        embed = discord.Embed(title="🔐 Verify config", color=discord.Color.blurple())
        embed.add_field(name="Access role", value=role.mention if role else "—", inline=True)
        embed.add_field(name="Threshold", value=f"{cfg['threshold']:,}", inline=True)
        embed.add_field(name="Category", value=cat.mention if cat else "—", inline=True)
        embed.add_field(name="Staff role", value=staff.mention if staff else "—", inline=True)
        embed.add_field(name="Log channel", value=ch.mention if ch else "—", inline=True)
        await ctx.send(embed=embed)

    @verify.command(name="resetinvite")
    async def v_reset(self, ctx, source_guild_id: int):
        result = await self.bot.db.execute(
            "DELETE FROM verify_used_invites WHERE guild_id=$1 AND source_guild_id=$2",
            ctx.guild.id, source_guild_id,
        )
        n = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        await ctx.send(f"✅ Cleared {n} used-server record(s) for `{source_guild_id}`.")


async def setup(bot):
    await bot.add_cog(Verify(bot))
