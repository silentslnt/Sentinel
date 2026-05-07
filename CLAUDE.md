# Sentinel — Claude Context

## What this project is
Personal Discord bot. Utility/automation only — no fun commands, no social media, no music, no levels, no economy. Modeled after Bleed bot's style and command feel.

## Stack
- discord.py 2.5+
- PostgreSQL via asyncpg
- Deployed on Railway (private GitHub repo)
- Python 3.11+

## Architecture
- `bot.py` — entrypoint, loads cogs, slash allowlist, prefix resolver
- `cogs/` — one file per feature; each cog owns its DB schema (created in `cog_load`)
- `utils/embed_script.py` — Bleed-style embed DSL parser (`{key: value}$v{key: value}`)
- `utils/database.py` — asyncpg pool wrapper
- `utils/config.py` — per-guild prefix cache

## Embed script syntax (Bleed-style)
Parameters separated by `$v`. Blocks: `{key: value}`.
- Simple: `title`, `description`, `color`, `image`, `thumbnail`, `url`, `timestamp`
- Compound: `{author: name && icon && url}`, `{footer: text && icon}`, `{field: name && value && inline}`, `{button: style && label && url}`
- Variables: `{user}`, `{user.mention}`, `{user.name}`, `{user.id}`, `{user.tag}`, `{user.avatar}`, `{user.created_at}`, `{user.joined_at}`, `{guild.name}`, `{guild.id}`, `{guild.count}`, `{guild.icon}`, `{guild.owner_id}`, `{channel.mention}`, `{channel.name}`, `{channel.id}`, `{inviter}`, `{inviter.mention}`, `{inviter.name}`, `{inviter.id}`, `{inviter.tag}`, `{inviter.avatar}`, `{invite.code}`
- Colors: hex string e.g. `ffffff` (no `#` needed, but `#` is stripped)

## Invite tracking flow
1. `cogs/invites.py` (InviteTracker) caches guild invites on startup
2. On `on_member_join`: diffs cache to find which invite was used → stores to DB → dispatches `on_member_join_tracked(member, inviter, invite_code)`
3. `cogs/system_messages.py` listens to `on_member_join_tracked` for welcome messages (gets inviter info)
4. `cogs/greet.py` listens to `on_member_join_tracked` for per-channel greets
5. **Load order matters**: `cogs.invites` must appear before `system_messages` and `greet` in `INITIAL_COGS`

## Cog list (INITIAL_COGS order)
configure, moderation, role_manager, channel_manager, utility, afk, snipe, guildlock, vanity, **invites**, **system_messages**, **greet**, booster, embeds, forms, custom_commands, sticky, tickets, verify, customize, logging, counters, autoresponder, crypto, help

## Slash command allowlist
Only these are in the slash menu (rest are prefix-only): help, afk, snipe, editsnipe, crypto, ban, kick, unban, mute, unmute, warn, purge, clear, slowmode, lock, unlock, nuke, add, claim, unclaim, close, transcript

## Env vars
```
DISCORD_TOKEN=
OWNER_ID=
DEFAULT_PREFIX=.
DATABASE_URL=postgres://...
```

## Database tables (per-cog ownership)
- `guild_prefixes` — config.py
- `system_messages` — system_messages.py (welcome/goodbye/boost)
- `invite_tracking` — invites.py (who invited who)
- `invite_adjustments` — invites.py (fake/bonus counts)
- `greet_channels` — greet.py (per-channel greet config)
- `saved_embeds`, `embed_buttons` — embeds.py
- `log_routes`, `log_ignores` — logging.py
- `verify_config`, `verify_used_invites`, `verify_tickets` — verify.py

## Principles
- No overcomplications. Simple, direct commands.
- No unused features or abstractions.
- Bleed-style: clean prefix commands, minimal slash exposure.
- Railway deployment — no local DB needed, use DATABASE_URL env var.
