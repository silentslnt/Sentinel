const {
  Client, GatewayIntentBits, Partials,
  EmbedBuilder, ActionRowBuilder, ButtonBuilder, ButtonStyle,
  PermissionFlagsBits, ChannelType, Collection,
  REST, Routes, SlashCommandBuilder,
} = require('discord.js');
const fs   = require('fs');
const path = require('path');

// ── ENV ───────────────────────────────────────────────────────────
const BOT_TOKEN  = process.env.BOT_TOKEN;
const CLIENT_ID  = process.env.CLIENT_ID;
const GUILD_ID   = process.env.GUILD_ID;
const PREFIX     = process.env.PREFIX || '!sg';

// ── DATA FILES ────────────────────────────────────────────────────
const DATA_DIR = path.join(__dirname, 'data');
if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR);

function loadJSON(file, def = {}) {
  const p = path.join(DATA_DIR, file);
  try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch { return def; }
}
function saveJSON(file, data) {
  fs.writeFileSync(path.join(DATA_DIR, file), JSON.stringify(data, null, 2));
}

// ── CLIENT ────────────────────────────────────────────────────────
const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMembers,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.GuildMessageReactions,
    GatewayIntentBits.GuildModeration,
    GatewayIntentBits.MessageContent,
    GatewayIntentBits.DirectMessages,
  ],
  partials: [Partials.Message, Partials.Channel, Partials.GuildMember],
});

// ── STICKY MESSAGES ───────────────────────────────────────────────
// { channelId: { content, embedColor, lastMsgId } }
let stickyData = loadJSON('sticky.json', {});
const stickyThrottle = new Map(); // channelId → timeout

async function repostSticky(channel) {
  const sticky = stickyData[channel.id];
  if (!sticky) return;
  try {
    if (sticky.lastMsgId) {
      const old = await channel.messages.fetch(sticky.lastMsgId).catch(() => null);
      if (old) await old.delete().catch(() => {});
    }
    const embed = new EmbedBuilder()
      .setDescription(sticky.content)
      .setColor(sticky.color || 0xFF10F0)
      .setFooter({ text: '📌 Sticky Message' });
    const sent = await channel.send({ embeds: [embed] });
    sticky.lastMsgId = sent.id;
    saveJSON('sticky.json', stickyData);
  } catch (e) { console.error('[sticky] repost error:', e.message); }
}

// ── AUTORESPONDERS ────────────────────────────────────────────────
// { guildId: [ { trigger, response, matchType:'exact'|'contains'|'startsWith', enabled } ] }
let autoresponders = loadJSON('autoresponders.json', {});

// ── AUTOROLES ─────────────────────────────────────────────────────
// { guildId: { onJoin: [roleId], onVerify: [roleId] } }
let autoroles = loadJSON('autoroles.json', {});

// ── MODERATION LOGS ───────────────────────────────────────────────
// { guildId: { logChannelId, cases: [ { caseId, type, userId, modId, reason, timestamp } ] } }
let modData = loadJSON('moderation.json', {});

function getModConfig(guildId) {
  if (!modData[guildId]) modData[guildId] = { logChannelId: null, cases: [] };
  return modData[guildId];
}
function nextCaseId(guildId) {
  const cfg = getModConfig(guildId);
  return (cfg.cases.length + 1);
}
async function logMod(guild, type, target, mod, reason, extra = {}) {
  const cfg = getModConfig(guild.id);
  const caseId = nextCaseId(guild.id);
  cfg.cases.push({ caseId, type, userId: target.id, modId: mod.id, reason, timestamp: new Date().toISOString(), ...extra });
  saveJSON('moderation.json', modData);

  if (!cfg.logChannelId) return;
  try {
    const ch = await guild.channels.fetch(cfg.logChannelId);
    const colors = { ban: 0xFF0000, kick: 0xFF6600, mute: 0xFFAA00, warn: 0xFFFF00, unmute: 0x00CC88, unban: 0x00CC88 };
    const embed = new EmbedBuilder()
      .setColor(colors[type] || 0x888888)
      .setTitle(`${type.toUpperCase()} — Case #${caseId}`)
      .addFields(
        { name: '👤 User',      value: `${target.tag || target.user?.tag || target.id} (${target.id})`, inline: true },
        { name: '🔨 Moderator', value: `${mod.tag || mod.user?.tag || mod.id}`, inline: true },
        { name: '📋 Reason',    value: reason || 'No reason provided', inline: false },
      )
      .setTimestamp();
    if (extra.duration) embed.addFields({ name: '⏱️ Duration', value: extra.duration, inline: true });
    await ch.send({ embeds: [embed] });
  } catch (e) { console.error('[mod log]', e.message); }
}

// ── WARNINGS ──────────────────────────────────────────────────────
// { guildId: { userId: [ { reason, modId, timestamp } ] } }
let warnings = loadJSON('warnings.json', {});

function getWarnings(guildId, userId) {
  if (!warnings[guildId]) warnings[guildId] = {};
  if (!warnings[guildId][userId]) warnings[guildId][userId] = [];
  return warnings[guildId][userId];
}

// ── MUTES (timeout tracking) ──────────────────────────────────────
function parseDuration(str) {
  const match = str.match(/^(\d+)(s|m|h|d)$/i);
  if (!match) return null;
  const n = parseInt(match[1]);
  const unit = match[2].toLowerCase();
  const ms = { s: 1000, m: 60000, h: 3600000, d: 86400000 }[unit];
  return { ms: n * ms, label: `${n}${unit}` };
}

// ═══════════════════════════════════════════════════════════════════
// EVENTS
// ═══════════════════════════════════════════════════════════════════

client.once('ready', () => {
  console.log(`✅ SILV GUARD online as ${client.user.tag}`);
  client.user.setPresence({ activities: [{ name: '🛡️ Protecting the server' }], status: 'online' });
  registerSlashCommands();
});

// ── AUTO ROLE ON JOIN ─────────────────────────────────────────────
client.on('guildMemberAdd', async (member) => {
  const cfg = autoroles[member.guild.id];
  if (!cfg?.onJoin?.length) return;
  for (const roleId of cfg.onJoin) {
    const role = member.guild.roles.cache.get(roleId);
    if (role) await member.roles.add(role).catch(() => {});
  }
});

// ── MESSAGES: sticky + autoresponder ─────────────────────────────
client.on('messageCreate', async (msg) => {
  if (msg.author.bot) return;
  if (!msg.guild) return;

  // ── Sticky: repost after non-bot message, throttled to 2s ──────
  if (stickyData[msg.channel.id]) {
    const prev = stickyThrottle.get(msg.channel.id);
    if (prev) clearTimeout(prev);
    const t = setTimeout(() => repostSticky(msg.channel), 2000);
    stickyThrottle.set(msg.channel.id, t);
  }

  // ── Autoresponders ─────────────────────────────────────────────
  const ars = autoresponders[msg.guild.id] || [];
  const content = msg.content.toLowerCase();
  for (const ar of ars) {
    if (!ar.enabled) continue;
    const trigger = ar.trigger.toLowerCase();
    const matched =
      (ar.matchType === 'exact'      && content === trigger) ||
      (ar.matchType === 'contains'   && content.includes(trigger)) ||
      (ar.matchType === 'startsWith' && content.startsWith(trigger));
    if (matched) {
      await msg.channel.send(ar.response).catch(() => {});
      break;
    }
  }

  // ── Prefix commands ────────────────────────────────────────────
  if (!msg.content.startsWith(PREFIX)) return;
  const isAdmin = msg.member?.permissions.has(PermissionFlagsBits.ManageGuild);
  if (!isAdmin) return;

  const args = msg.content.slice(PREFIX.length).trim().split(/\s+/);
  const cmd  = args.shift().toLowerCase();

  // ── !sg sticky set <color?> <content> ─────────────────────────
  if (cmd === 'sticky') {
    const sub = args.shift()?.toLowerCase();

    if (sub === 'set') {
      // optional hex color as first arg
      let color = 0xFF10F0;
      if (/^#?[0-9a-f]{6}$/i.test(args[0])) {
        color = parseInt(args.shift().replace('#', ''), 16);
      }
      const content = args.join(' ');
      if (!content) return msg.reply('Usage: `!sg sticky set [#hexcolor] <message>`');
      stickyData[msg.channel.id] = { content, color, lastMsgId: null };
      saveJSON('sticky.json', stickyData);
      await repostSticky(msg.channel);
      msg.reply('📌 Sticky message set!').then(m => setTimeout(() => m.delete().catch(() => {}), 4000));
    }
    else if (sub === 'remove' || sub === 'clear') {
      const sticky = stickyData[msg.channel.id];
      if (sticky?.lastMsgId) {
        const old = await msg.channel.messages.fetch(sticky.lastMsgId).catch(() => null);
        if (old) await old.delete().catch(() => {});
      }
      delete stickyData[msg.channel.id];
      saveJSON('sticky.json', stickyData);
      msg.reply('🗑️ Sticky removed.').then(m => setTimeout(() => m.delete().catch(() => {}), 4000));
    }
    else {
      msg.reply('Usage: `!sg sticky set [#color] <text>` · `!sg sticky remove`');
    }
    return;
  }

  // ── !sg ar add <exact|contains|startswith> <trigger> | <response>
  if (cmd === 'ar') {
    const sub = args.shift()?.toLowerCase();
    if (!autoresponders[msg.guild.id]) autoresponders[msg.guild.id] = [];

    if (sub === 'add') {
      const matchType = args.shift()?.toLowerCase();
      if (!['exact','contains','startswith'].includes(matchType))
        return msg.reply('Match type must be `exact`, `contains`, or `startswith`');
      const rest = args.join(' ');
      const parts = rest.split('|');
      if (parts.length < 2) return msg.reply('Usage: `!sg ar add <matchType> <trigger> | <response>`');
      const trigger  = parts[0].trim();
      const response = parts.slice(1).join('|').trim();
      autoresponders[msg.guild.id].push({ trigger, response, matchType: matchType === 'startswith' ? 'startsWith' : matchType, enabled: true });
      saveJSON('autoresponders.json', autoresponders);
      msg.reply(`✅ Autoresponder added: \`${trigger}\` (${matchType})`);
    }
    else if (sub === 'remove' || sub === 'delete') {
      const trigger = args.join(' ').toLowerCase();
      const before  = autoresponders[msg.guild.id].length;
      autoresponders[msg.guild.id] = autoresponders[msg.guild.id].filter(a => a.trigger.toLowerCase() !== trigger);
      saveJSON('autoresponders.json', autoresponders);
      msg.reply(autoresponders[msg.guild.id].length < before ? `🗑️ Removed \`${trigger}\`` : '❌ Trigger not found.');
    }
    else if (sub === 'list') {
      const ars = autoresponders[msg.guild.id];
      if (!ars?.length) return msg.reply('No autoresponders set.');
      const embed = new EmbedBuilder().setColor(0xFF10F0).setTitle('🤖 Autoresponders');
      ars.forEach((a, i) => embed.addFields({ name: `#${i+1} [${a.matchType}] \`${a.trigger}\``, value: a.response.slice(0, 200), inline: false }));
      msg.channel.send({ embeds: [embed] });
    }
    else if (sub === 'toggle') {
      const trigger = args.join(' ').toLowerCase();
      const ar = autoresponders[msg.guild.id]?.find(a => a.trigger.toLowerCase() === trigger);
      if (!ar) return msg.reply('❌ Trigger not found.');
      ar.enabled = !ar.enabled;
      saveJSON('autoresponders.json', autoresponders);
      msg.reply(`${ar.enabled ? '✅ Enabled' : '⏸️ Disabled'}: \`${trigger}\``);
    }
    else {
      msg.reply('Usage: `!sg ar add <exact|contains|startswith> <trigger> | <response>` · `!sg ar list` · `!sg ar remove <trigger>` · `!sg ar toggle <trigger>`');
    }
    return;
  }

  // ── !sg autorole add/remove/list <join|leave> <@role> ─────────
  if (cmd === 'autorole') {
    const sub = args.shift()?.toLowerCase();
    if (!autoroles[msg.guild.id]) autoroles[msg.guild.id] = { onJoin: [] };

    if (sub === 'add') {
      const roleId = args[0]?.replace(/\D/g,'');
      const role   = msg.guild.roles.cache.get(roleId);
      if (!role) return msg.reply('❌ Role not found. Mention the role or use its ID.');
      if (!autoroles[msg.guild.id].onJoin.includes(role.id)) {
        autoroles[msg.guild.id].onJoin.push(role.id);
        saveJSON('autoroles.json', autoroles);
      }
      msg.reply(`✅ **${role.name}** will be given to new members on join.`);
    }
    else if (sub === 'remove') {
      const roleId = args[0]?.replace(/\D/g,'');
      autoroles[msg.guild.id].onJoin = autoroles[msg.guild.id].onJoin.filter(id => id !== roleId);
      saveJSON('autoroles.json', autoroles);
      msg.reply('🗑️ Autorole removed.');
    }
    else if (sub === 'list') {
      const roles = autoroles[msg.guild.id]?.onJoin || [];
      if (!roles.length) return msg.reply('No autoroles set.');
      msg.reply(`**Join autoroles:** ${roles.map(id => `<@&${id}>`).join(', ')}`);
    }
    else {
      msg.reply('Usage: `!sg autorole add <@role>` · `!sg autorole remove <@role>` · `!sg autorole list`');
    }
    return;
  }

  // ── !sg logchannel <#channel> ─────────────────────────────────
  if (cmd === 'logchannel') {
    const channelId = args[0]?.replace(/\D/g,'');
    const ch = msg.guild.channels.cache.get(channelId);
    if (!ch) return msg.reply('❌ Channel not found.');
    getModConfig(msg.guild.id).logChannelId = ch.id;
    saveJSON('moderation.json', modData);
    msg.reply(`✅ Mod logs will be sent to <#${ch.id}>`);
    return;
  }

  // ── !sg help ──────────────────────────────────────────────────
  if (cmd === 'help') {
    const embed = new EmbedBuilder()
      .setColor(0xFF10F0)
      .setTitle('🛡️ SILV GUARD — Commands')
      .setDescription(`Prefix: \`${PREFIX}\` · All require **Manage Server**`)
      .addFields(
        { name: '📌 Sticky', value: '`!sg sticky set [#color] <text>` — pin a sticky message\n`!sg sticky remove` — remove it', inline: false },
        { name: '🤖 Autoresponders', value: '`!sg ar add <exact|contains|startswith> <trigger> | <response>`\n`!sg ar list` · `!sg ar remove <trigger>` · `!sg ar toggle <trigger>`', inline: false },
        { name: '🎭 Autoroles', value: '`!sg autorole add <@role>` — give role on join\n`!sg autorole remove <@role>` · `!sg autorole list`', inline: false },
        { name: '🔨 Moderation (slash commands)', value: '`/ban` `/kick` `/mute` `/unmute` `/warn` `/warnings` `/purge` `/lock` `/unlock`', inline: false },
        { name: '⚙️ Config', value: '`!sg logchannel <#channel>` — set mod log channel', inline: false },
      );
    msg.channel.send({ embeds: [embed] });
    return;
  }
});

// ═══════════════════════════════════════════════════════════════════
// SLASH COMMANDS
// ═══════════════════════════════════════════════════════════════════

const slashCommands = [
  new SlashCommandBuilder().setName('ban').setDescription('Ban a member')
    .addUserOption(o => o.setName('user').setDescription('User to ban').setRequired(true))
    .addStringOption(o => o.setName('reason').setDescription('Reason'))
    .addIntegerOption(o => o.setName('delete_days').setDescription('Days of messages to delete (0-7)').setMinValue(0).setMaxValue(7))
    .setDefaultMemberPermissions(PermissionFlagsBits.BanMembers),

  new SlashCommandBuilder().setName('unban').setDescription('Unban a user by ID')
    .addStringOption(o => o.setName('user_id').setDescription('User ID').setRequired(true))
    .addStringOption(o => o.setName('reason').setDescription('Reason'))
    .setDefaultMemberPermissions(PermissionFlagsBits.BanMembers),

  new SlashCommandBuilder().setName('kick').setDescription('Kick a member')
    .addUserOption(o => o.setName('user').setDescription('User to kick').setRequired(true))
    .addStringOption(o => o.setName('reason').setDescription('Reason'))
    .setDefaultMemberPermissions(PermissionFlagsBits.KickMembers),

  new SlashCommandBuilder().setName('mute').setDescription('Timeout a member')
    .addUserOption(o => o.setName('user').setDescription('User to mute').setRequired(true))
    .addStringOption(o => o.setName('duration').setDescription('Duration: 10m, 1h, 1d (max 28d)').setRequired(true))
    .addStringOption(o => o.setName('reason').setDescription('Reason'))
    .setDefaultMemberPermissions(PermissionFlagsBits.ModerateMembers),

  new SlashCommandBuilder().setName('unmute').setDescription('Remove timeout from a member')
    .addUserOption(o => o.setName('user').setDescription('User to unmute').setRequired(true))
    .addStringOption(o => o.setName('reason').setDescription('Reason'))
    .setDefaultMemberPermissions(PermissionFlagsBits.ModerateMembers),

  new SlashCommandBuilder().setName('warn').setDescription('Warn a member')
    .addUserOption(o => o.setName('user').setDescription('User to warn').setRequired(true))
    .addStringOption(o => o.setName('reason').setDescription('Reason').setRequired(true))
    .setDefaultMemberPermissions(PermissionFlagsBits.ManageMessages),

  new SlashCommandBuilder().setName('warnings').setDescription('View warnings for a user')
    .addUserOption(o => o.setName('user').setDescription('User').setRequired(true))
    .setDefaultMemberPermissions(PermissionFlagsBits.ManageMessages),

  new SlashCommandBuilder().setName('clearwarnings').setDescription('Clear all warnings for a user')
    .addUserOption(o => o.setName('user').setDescription('User').setRequired(true))
    .setDefaultMemberPermissions(PermissionFlagsBits.ManageGuild),

  new SlashCommandBuilder().setName('purge').setDescription('Delete messages in bulk')
    .addIntegerOption(o => o.setName('amount').setDescription('Number of messages (1–100)').setRequired(true).setMinValue(1).setMaxValue(100))
    .addUserOption(o => o.setName('user').setDescription('Only delete messages from this user'))
    .setDefaultMemberPermissions(PermissionFlagsBits.ManageMessages),

  new SlashCommandBuilder().setName('lock').setDescription('Lock a channel (no one can send messages)')
    .addChannelOption(o => o.setName('channel').setDescription('Channel to lock (defaults to current)'))
    .addStringOption(o => o.setName('reason').setDescription('Reason'))
    .setDefaultMemberPermissions(PermissionFlagsBits.ManageChannels),

  new SlashCommandBuilder().setName('unlock').setDescription('Unlock a channel')
    .addChannelOption(o => o.setName('channel').setDescription('Channel to unlock (defaults to current)'))
    .setDefaultMemberPermissions(PermissionFlagsBits.ManageChannels),

  new SlashCommandBuilder().setName('slowmode').setDescription('Set slowmode on a channel')
    .addIntegerOption(o => o.setName('seconds').setDescription('Seconds (0 = off)').setRequired(true).setMinValue(0).setMaxValue(21600))
    .addChannelOption(o => o.setName('channel').setDescription('Channel (defaults to current)'))
    .setDefaultMemberPermissions(PermissionFlagsBits.ManageChannels),

  new SlashCommandBuilder().setName('userinfo').setDescription('Get info about a user')
    .addUserOption(o => o.setName('user').setDescription('User (defaults to yourself)')),

  new SlashCommandBuilder().setName('serverinfo').setDescription('Get info about this server'),

  new SlashCommandBuilder().setName('cases').setDescription('View mod cases for a user')
    .addUserOption(o => o.setName('user').setDescription('User').setRequired(true))
    .setDefaultMemberPermissions(PermissionFlagsBits.ManageMessages),
];

async function registerSlashCommands() {
  try {
    const rest = new REST({ version: '10' }).setToken(BOT_TOKEN);
    await rest.put(Routes.applicationGuildCommands(CLIENT_ID, GUILD_ID), {
      body: slashCommands.map(c => c.toJSON()),
    });
    console.log('✅ Slash commands registered');
  } catch (e) { console.error('Slash command registration failed:', e.message); }
}

// ── Slash command handler ─────────────────────────────────────────
client.on('interactionCreate', async (interaction) => {
  if (!interaction.isChatInputCommand()) return;
  const { commandName, guild, member } = interaction;

  // /ban
  if (commandName === 'ban') {
    const target = interaction.options.getMember('user');
    const reason = interaction.options.getString('reason') || 'No reason provided';
    const days   = interaction.options.getInteger('delete_days') ?? 0;
    if (!target) return interaction.reply({ content: '❌ User not found.', ephemeral: true });
    if (!target.bannable) return interaction.reply({ content: '❌ I cannot ban this user.', ephemeral: true });
    await target.ban({ reason, deleteMessageDays: days });
    await logMod(guild, 'ban', target.user, member.user, reason);
    interaction.reply({ embeds: [new EmbedBuilder().setColor(0xFF0000).setDescription(`🔨 **${target.user.tag}** has been banned.\n📋 Reason: ${reason}`)] });
    return;
  }

  // /unban
  if (commandName === 'unban') {
    const userId = interaction.options.getString('user_id');
    const reason = interaction.options.getString('reason') || 'No reason provided';
    try {
      const ban = await guild.bans.fetch(userId);
      await guild.members.unban(userId, reason);
      await logMod(guild, 'unban', ban.user, member.user, reason);
      interaction.reply({ embeds: [new EmbedBuilder().setColor(0x00CC88).setDescription(`✅ **${ban.user.tag}** has been unbanned.`)] });
    } catch { interaction.reply({ content: '❌ Ban not found for that user ID.', ephemeral: true }); }
    return;
  }

  // /kick
  if (commandName === 'kick') {
    const target = interaction.options.getMember('user');
    const reason = interaction.options.getString('reason') || 'No reason provided';
    if (!target) return interaction.reply({ content: '❌ User not found.', ephemeral: true });
    if (!target.kickable) return interaction.reply({ content: '❌ I cannot kick this user.', ephemeral: true });
    await target.kick(reason);
    await logMod(guild, 'kick', target.user, member.user, reason);
    interaction.reply({ embeds: [new EmbedBuilder().setColor(0xFF6600).setDescription(`👢 **${target.user.tag}** has been kicked.\n📋 Reason: ${reason}`)] });
    return;
  }

  // /mute
  if (commandName === 'mute') {
    const target   = interaction.options.getMember('user');
    const durStr   = interaction.options.getString('duration');
    const reason   = interaction.options.getString('reason') || 'No reason provided';
    const dur      = parseDuration(durStr);
    if (!target) return interaction.reply({ content: '❌ User not found.', ephemeral: true });
    if (!dur)    return interaction.reply({ content: '❌ Invalid duration. Use `10m`, `1h`, `2d` etc.', ephemeral: true });
    if (dur.ms > 28 * 86400000) return interaction.reply({ content: '❌ Max timeout is 28 days.', ephemeral: true });
    await target.timeout(dur.ms, reason);
    await logMod(guild, 'mute', target.user, member.user, reason, { duration: dur.label });
    interaction.reply({ embeds: [new EmbedBuilder().setColor(0xFFAA00).setDescription(`🔇 **${target.user.tag}** muted for **${dur.label}**.\n📋 Reason: ${reason}`)] });
    return;
  }

  // /unmute
  if (commandName === 'unmute') {
    const target = interaction.options.getMember('user');
    const reason = interaction.options.getString('reason') || 'No reason provided';
    if (!target) return interaction.reply({ content: '❌ User not found.', ephemeral: true });
    await target.timeout(null, reason);
    await logMod(guild, 'unmute', target.user, member.user, reason);
    interaction.reply({ embeds: [new EmbedBuilder().setColor(0x00CC88).setDescription(`🔊 **${target.user.tag}** has been unmuted.`)] });
    return;
  }

  // /warn
  if (commandName === 'warn') {
    const target = interaction.options.getMember('user');
    const reason = interaction.options.getString('reason');
    if (!target) return interaction.reply({ content: '❌ User not found.', ephemeral: true });
    const userWarns = getWarnings(guild.id, target.id);
    userWarns.push({ reason, modId: member.id, timestamp: new Date().toISOString() });
    warnings[guild.id][target.id] = userWarns;
    saveJSON('warnings.json', warnings);
    await logMod(guild, 'warn', target.user, member.user, reason);
    // DM the user
    target.user.send(`⚠️ You have been warned in **${guild.name}**.\n📋 Reason: ${reason}\n\nThis is warning **#${userWarns.length}**.`).catch(() => {});
    interaction.reply({ embeds: [new EmbedBuilder().setColor(0xFFFF00).setDescription(`⚠️ **${target.user.tag}** warned (total: **${userWarns.length}** warnings).\n📋 Reason: ${reason}`)] });
    return;
  }

  // /warnings
  if (commandName === 'warnings') {
    const target = interaction.options.getUser('user');
    const userWarns = getWarnings(guild.id, target.id);
    if (!userWarns.length) return interaction.reply({ content: `✅ **${target.tag}** has no warnings.`, ephemeral: true });
    const embed = new EmbedBuilder().setColor(0xFFFF00).setTitle(`⚠️ Warnings — ${target.tag}`).setDescription(`Total: **${userWarns.length}**`);
    userWarns.slice(-10).forEach((w, i) => {
      embed.addFields({ name: `#${i+1} — ${new Date(w.timestamp).toLocaleDateString()}`, value: w.reason, inline: false });
    });
    interaction.reply({ embeds: [embed], ephemeral: true });
    return;
  }

  // /clearwarnings
  if (commandName === 'clearwarnings') {
    const target = interaction.options.getUser('user');
    if (warnings[guild.id]) warnings[guild.id][target.id] = [];
    saveJSON('warnings.json', warnings);
    interaction.reply({ content: `✅ Cleared all warnings for **${target.tag}**.`, ephemeral: true });
    return;
  }

  // /purge
  if (commandName === 'purge') {
    const amount = interaction.options.getInteger('amount');
    const user   = interaction.options.getUser('user');
    await interaction.deferReply({ ephemeral: true });
    let msgs = await interaction.channel.messages.fetch({ limit: 100 });
    if (user) msgs = msgs.filter(m => m.author.id === user.id);
    const toDelete = [...msgs.values()].slice(0, amount);
    const deleted  = await interaction.channel.bulkDelete(toDelete, true).catch(() => new Collection());
    interaction.editReply(`🗑️ Deleted **${deleted.size}** message${deleted.size !== 1 ? 's' : ''}.`);
    return;
  }

  // /lock
  if (commandName === 'lock') {
    const ch     = interaction.options.getChannel('channel') || interaction.channel;
    const reason = interaction.options.getString('reason') || 'Channel locked by staff';
    await ch.permissionOverwrites.edit(guild.roles.everyone, { SendMessages: false });
    interaction.reply({ embeds: [new EmbedBuilder().setColor(0xFF0000).setDescription(`🔒 <#${ch.id}> has been **locked**.\n📋 ${reason}`)] });
    return;
  }

  // /unlock
  if (commandName === 'unlock') {
    const ch = interaction.options.getChannel('channel') || interaction.channel;
    await ch.permissionOverwrites.edit(guild.roles.everyone, { SendMessages: null });
    interaction.reply({ embeds: [new EmbedBuilder().setColor(0x00CC88).setDescription(`🔓 <#${ch.id}> has been **unlocked**.`)] });
    return;
  }

  // /slowmode
  if (commandName === 'slowmode') {
    const seconds = interaction.options.getInteger('seconds');
    const ch      = interaction.options.getChannel('channel') || interaction.channel;
    await ch.setRateLimitPerUser(seconds);
    interaction.reply({ embeds: [new EmbedBuilder().setColor(0x5865F2).setDescription(seconds === 0 ? `✅ Slowmode removed from <#${ch.id}>.` : `⏱️ Slowmode set to **${seconds}s** in <#${ch.id}>.`)] });
    return;
  }

  // /userinfo
  if (commandName === 'userinfo') {
    const user   = interaction.options.getUser('user') || interaction.user;
    const member = guild.members.cache.get(user.id);
    const embed  = new EmbedBuilder()
      .setColor(0x5865F2)
      .setTitle(user.tag)
      .setThumbnail(user.displayAvatarURL({ dynamic: true }))
      .addFields(
        { name: '🆔 ID',         value: user.id, inline: true },
        { name: '📅 Created',    value: `<t:${Math.floor(user.createdTimestamp/1000)}:R>`, inline: true },
        { name: '📥 Joined',     value: member ? `<t:${Math.floor(member.joinedTimestamp/1000)}:R>` : 'N/A', inline: true },
        { name: '🎭 Roles',      value: member ? member.roles.cache.filter(r=>r.id!==guild.id).map(r=>`<@&${r.id}>`).join(', ')||'None' : 'N/A', inline: false },
        { name: '⚠️ Warnings',   value: `${getWarnings(guild.id, user.id).length}`, inline: true },
      );
    interaction.reply({ embeds: [embed], ephemeral: true });
    return;
  }

  // /serverinfo
  if (commandName === 'serverinfo') {
    const g = guild;
    await g.fetch();
    const embed = new EmbedBuilder()
      .setColor(0xFF10F0)
      .setTitle(g.name)
      .setThumbnail(g.iconURL({ dynamic: true }))
      .addFields(
        { name: '👑 Owner',     value: `<@${g.ownerId}>`, inline: true },
        { name: '👥 Members',   value: `${g.memberCount}`, inline: true },
        { name: '📅 Created',   value: `<t:${Math.floor(g.createdTimestamp/1000)}:R>`, inline: true },
        { name: '💬 Channels',  value: `${g.channels.cache.size}`, inline: true },
        { name: '🎭 Roles',     value: `${g.roles.cache.size}`, inline: true },
        { name: '😀 Emojis',    value: `${g.emojis.cache.size}`, inline: true },
        { name: '🔒 Verification', value: g.verificationLevel.toString(), inline: true },
      );
    interaction.reply({ embeds: [embed] });
    return;
  }

  // /cases
  if (commandName === 'cases') {
    const target = interaction.options.getUser('user');
    const cfg    = getModConfig(guild.id);
    const cases  = cfg.cases.filter(c => c.userId === target.id);
    if (!cases.length) return interaction.reply({ content: `✅ No mod cases for **${target.tag}**.`, ephemeral: true });
    const embed = new EmbedBuilder().setColor(0xFF10F0).setTitle(`📋 Cases — ${target.tag}`).setDescription(`Total: **${cases.length}**`);
    cases.slice(-10).forEach(c => {
      embed.addFields({ name: `Case #${c.caseId} — ${c.type.toUpperCase()}`, value: `📋 ${c.reason || 'No reason'} · <t:${Math.floor(new Date(c.timestamp).getTime()/1000)}:R>`, inline: false });
    });
    interaction.reply({ embeds: [embed], ephemeral: true });
    return;
  }
});

// ── Login ─────────────────────────────────────────────────────────
client.login(BOT_TOKEN);
