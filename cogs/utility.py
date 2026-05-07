import discord
from discord.ext import commands

from utils.checks import with_perms
import asyncio
import platform
import psutil
import os

class Utility(commands.Cog):
    """⚙️ Utility commands for server and bot information"""
    
    def __init__(self, bot):
        self.bot = bot
    
    @commands.command(aliases=['user', 'whois'])
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def userinfo(self, ctx, member: discord.Member = None):
        """Get information about a user"""
        member = member or ctx.author
        
        roles = [role.mention for role in member.roles[1:]]  # Exclude @everyone
        roles_str = ", ".join(roles) if roles else "None"
        
        embed = discord.Embed(
            title=f"User Info - {member}",
            color=member.color
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        
        embed.add_field(name="ID", value=member.id, inline=True)
        embed.add_field(name="Nickname", value=member.nick or "None", inline=True)
        embed.add_field(name="Status", value=str(member.status).title(), inline=True)
        
        embed.add_field(
            name="Account Created",
            value=f"<t:{int(member.created_at.timestamp())}:R>",
            inline=True
        )
        embed.add_field(
            name="Joined Server",
            value=f"<t:{int(member.joined_at.timestamp())}:R>",
            inline=True
        )
        embed.add_field(name="Bot?", value="Yes" if member.bot else "No", inline=True)
        
        embed.add_field(name=f"Roles [{len(roles)}]", value=roles_str[:1024], inline=False)
        
        await ctx.send(embed=embed)
    
    @commands.command(aliases=['server', 'guild'])
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def serverinfo(self, ctx):
        """Get information about the server"""
        guild = ctx.guild
        
        embed = discord.Embed(
            title=f"Server Info - {guild.name}",
            color=discord.Color.blue()
        )
        
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        
        embed.add_field(name="Server ID", value=guild.id, inline=True)
        embed.add_field(name="Owner", value=guild.owner.mention, inline=True)
        embed.add_field(
            name="Created",
            value=f"<t:{int(guild.created_at.timestamp())}:R>",
            inline=True
        )
        
        embed.add_field(name="Members", value=guild.member_count, inline=True)
        embed.add_field(name="Channels", value=len(guild.channels), inline=True)
        embed.add_field(name="Roles", value=len(guild.roles), inline=True)
        
        embed.add_field(name="Emojis", value=len(guild.emojis), inline=True)
        embed.add_field(name="Boost Level", value=guild.premium_tier, inline=True)
        embed.add_field(name="Boosts", value=guild.premium_subscription_count, inline=True)
        
        await ctx.send(embed=embed)
    
    @commands.command(aliases=['botinfo', 'info', 'stats'])
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def about(self, ctx):
        """Get information about the bot"""
        embed = discord.Embed(
            title=f"{self.bot.user.name} Information",
            description="A multi-purpose Discord bot with economy, moderation, and fun features!",
            color=discord.Color.blue()
        )
        
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        
        # Bot stats
        total_members = sum(guild.member_count for guild in self.bot.guilds)
        total_channels = sum(len(guild.channels) for guild in self.bot.guilds)
        
        embed.add_field(name="Servers", value=len(self.bot.guilds), inline=True)
        embed.add_field(name="Users", value=f"{total_members:,}", inline=True)
        embed.add_field(name="Channels", value=f"{total_channels:,}", inline=True)
        
        # System stats
        embed.add_field(
            name="Python Version",
            value=platform.python_version(),
            inline=True
        )
        embed.add_field(
            name="Discord.py Version",
            value=discord.__version__,
            inline=True
        )
        embed.add_field(
            name="Uptime",
            value=f"<t:{int(self.bot.start_time.timestamp())}:R>",
            inline=True
        )
        
        # Resource usage
        process = psutil.Process(os.getpid())
        mem_usage = process.memory_info().rss / 1024 ** 2  # Convert to MB
        cpu_usage = process.cpu_percent()
        
        embed.add_field(name="Memory Usage", value=f"{mem_usage:.2f} MB", inline=True)
        embed.add_field(name="CPU Usage", value=f"{cpu_usage}%", inline=True)
        embed.add_field(name="Commands", value=len(self.bot.commands), inline=True)
        
        embed.set_footer(text=f"Version {self.bot.config['version']}")
        
        await ctx.send(embed=embed)
    
    @commands.command()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def ping(self, ctx):
        """Check the bot's latency"""
        embed = discord.Embed(
            description=f"Latency: **{round(self.bot.latency * 1000)}ms**",
            color=discord.Color.default()
        )
        await ctx.send(embed=embed)
    
    @commands.command()
    async def invite(self, ctx):
        """Get the bot's invite link"""
        permissions = discord.Permissions(
            kick_members=True,
            ban_members=True,
            manage_channels=True,
            manage_roles=True,
            manage_messages=True,
            embed_links=True,
            attach_files=True,
            read_message_history=True,
            add_reactions=True,
            moderate_members=True
        )
        
        invite_url = discord.utils.oauth_url(
            self.bot.user.id,
            permissions=permissions
        )
        
        embed = discord.Embed(
            description=f"[Click here to invite me to your server]({invite_url})",
            color=discord.Color.default()
        )
        
        if self.bot.config.get('support_server'):
            embed.add_field(
                name="Support Server",
                value=f"[Join here]({self.bot.config['support_server']})"
            )
        
        await ctx.send(embed=embed)
    
    @commands.command(aliases=['poll'])
    @commands.cooldown(1, 30, commands.BucketType.user)
    @with_perms(manage_messages=True)
    async def createpoll(self, ctx, question, *options):
        """Create a poll (max 10 options)"""
        if len(options) > 10:
            return await ctx.send("❌ You can only have up to 10 options!")

        if len(options) < 2:
            return await ctx.send("❌ You need at least 2 options!")

        embed = discord.Embed(
            title="Poll",
            description=question,
            color=discord.Color.default()
        )
        
        reactions = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟']
        
        description = ""
        for idx, option in enumerate(options):
            description += f"\n{reactions[idx]} {option}"
        
        embed.add_field(name="Options", value=description, inline=False)
        embed.set_footer(text=f"Poll by {ctx.author}")
        
        poll_msg = await ctx.send(embed=embed)
        
        for idx in range(len(options)):
            await poll_msg.add_reaction(reactions[idx])
    
    @commands.command()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def remind(self, ctx, time: int, *, reminder):
        """Set a reminder (time in seconds)"""
        if time < 1 or time > 86400:  # Max 24 hours
            return await ctx.send("❌ Time must be between 1 second and 24 hours!")

        await ctx.send(f"Reminder set for {time} seconds.")
        
        await asyncio.sleep(time)
        
        embed = discord.Embed(
            title="⏰ Reminder!",
            description=reminder,
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"Requested {time} seconds ago")
        
        await ctx.send(f"{ctx.author.mention}", embed=embed)
    
async def setup(bot):
    await bot.add_cog(Utility(bot))