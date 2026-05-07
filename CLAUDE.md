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
- `utils/checks.py` — shared permission checks (see below)

## Permission system (utils/checks.py)

Three tiers:

| Check | Who passes |
|---|---|
| `is_guild_admin` | Server owner, anyone with Administrator perm, or whitelist |
| `is_whitelisted` | Server owner, OWNER_ID (env), or explicit whitelist — admin perm alone NOT enough |
| `with_perms(**p)` | Real Discord perm OR fake permission from fp system |

- `is_guild_admin` — used on most config commands (vanity, greet, sysmsg, embeds, etc.)
- `is_whitelisted` — used on `nuke`, `restrict`, `fp`, `disable/enable` (dangerous commands)
- `with_perms` — drop-in for `has_permissions` on moderation/role commands; checks fp cache first
- Admin whitelist stored in `admin_whitelist` table; managed via `,admin add/remove/list` (server owner only)

## Restrict / fp / disable system (cogs/restrictions.py)

Global bot check registered on load — runs before every command.

- `restrict <cmd> [@role]` / `rc` — lock command to a role (omit role to remove). Hierarchy-aware: restricting `role` also blocks `role add` etc. Aliases auto-covered via `qualified_name`.
- `fp add/remove/list @role <permission>` — grant a role a fake Discord permission so they pass `with_perms` checks without the real perm
- `disable/enable <cmd>` — toggle a command off/on for the guild. Also hierarchy-aware.
- `disabled` — list all disabled commands
- `restrictions` — list all role restrictions
- PROTECTED_ROOTS: `restrict`, `fakepermission`, `disable`, `enable`, `disabled`, `admin`, `restrictions`, `reload`, `sync`, `shutdown`, `prefix`, `configure` — these cannot be disabled or restricted

Bypass: server owner and OWNER_ID bypass all disable/restrict checks. Everyone else (including whitelisted admins) follows them.

## Embed script syntax (Bleed-style)
Parameters separated by `$v`. Blocks: `{key: value}`.
- Simple: `title`, `description`, `color`, `image`, `thumbnail`, `url`, `timestamp`
- Compound: `{author: name && icon && url}`, `{footer: text && icon}`, `{field: name && value && inline}`, `{button: style && label && url}`
- Variables: `{user}`, `{user.mention}`, `{user.name}`, `{user.id}`, `{user.tag}`, `{user.avatar}`, `{user.created_at}`, `{user.joined_at}`, `{guild.name}`, `{guild.id}`, `{guild.count}`, `{guild.icon}`, `{guild.owner_id}`, `{channel.mention}`, `{channel.name}`, `{channel.id}`, `{inviter}`, `{inviter.mention}`, `{inviter.name}`, `{inviter.id}`, `{inviter.tag}`, `{inviter.avatar}`, `{invite.code}`
- Colors: hex string e.g. `ffffff` (no `#` needed, but `#` is stripped)

## Embed commands (cogs/embeds.py)
- `embed post <#channel> <script>` — parse raw script and send directly, no saving
- `embed save <name> <script>` — save raw script as named embed
- `embed create <name>` — guided UI builder (12 buttons, live preview)
- `embed send <name> <#channel>` — send saved embed
- `embed edit <name> <script>` — replace script
- `embed raw <name>` — show raw script
- `embed preview <name>` — preview here
- `embed button add* / list / remove` — manage persistent buttons

Button types: `link` (URL), `role` (toggle role), `open` (open another embed ephemerally), `form`, `ticket`, `verify`

## Invite tracking flow
1. `cogs/invites.py` (InviteTracker) caches guild invites on startup
2. On `on_member_join`: diffs cache to find which invite was used → stores to DB → dispatches `on_member_join_tracked(member, inviter, invite_code)`
3. `cogs/system_messages.py` listens to `on_member_join_tracked` for welcome messages (gets inviter info)
4. `cogs/greet.py` listens to `on_member_join_tracked` for per-channel greets
5. **Load order matters**: `cogs.invites` must appear before `system_messages` and `greet` in `INITIAL_COGS`

## Preview/test commands
- `greettest [#channel]` — preview greet with yourself as test user
- `sysmsg test <welcome|goodbye|boost>` — preview in current channel
- `vanity test` — preview vanity award message

## Cog list (INITIAL_COGS order)
configure, **restrictions**, moderation, role_manager, channel_manager, utility, afk, snipe, guildlock, vanity, admin, **invites**, **system_messages**, **greet**, booster, embeds, forms, custom_commands, sticky, tickets, verify, customize, logging, counters, autoresponder, crypto, help

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
- `admin_whitelist` — admin.py (whitelist for is_guild_admin / is_whitelisted)
- `disabled_commands` — restrictions.py
- `command_restrictions` — restrictions.py
- `fake_permissions` — restrictions.py
- `system_messages` — system_messages.py (welcome/goodbye/boost)
- `invite_tracking` — invites.py (who invited who)
- `invite_adjustments` — invites.py (fake/bonus counts)
- `greet_channels` — greet.py (per-channel greet config)
- `saved_embeds`, `embed_buttons` — embeds.py
- `log_routes`, `log_ignores` — logging.py
- `verify_config`, `verify_used_invites`, `verify_tickets` — verify.py
- `vanity_config`, `vanity_roles`, `vanity_granted` — vanity.py
- `booster_config` — booster.py
- `sticky_messages` — sticky.py
- `autoresponders` — autoresponder.py
- `counters` — counters.py

## Principles
- No overcomplications. Simple, direct commands.
- No unused features or abstractions.
- Bleed-style: clean prefix commands, minimal slash exposure.
- Railway deployment — no local DB needed, use DATABASE_URL env var.
- Never add Co-Authored-By or "Generated with Claude" to commits.
