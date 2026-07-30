"""The bot's main Client subclass: one Gateway connection handling both
passive monitoring (MESSAGE_CREATE) and slash/context-menu commands, per
the mechanism decision in brief section 3.
"""

from __future__ import annotations

import discord
import httpx
from discord.ext import commands

from cratebot.config import Settings
from cratebot.db import Database
from cratebot.links.resolver import OdesliClient, YouTubeOEmbedClient
from cratebot.logging_setup import get_logger
from cratebot.pipeline import AddPipeline
from cratebot.spotify.auth import SpotifyAuth
from cratebot.spotify.client import SpotifyClient

logger = get_logger(__name__)


class Cratebot(commands.Bot):
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        http_client: httpx.AsyncClient,
        spotify_auth: SpotifyAuth,
        spotify: SpotifyClient,
        odesli: OdesliClient,
        oembed: YouTubeOEmbedClient,
        pipeline: AddPipeline,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.messages = True
        intents.reactions = True
        intents.members = False

        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

        self.settings = settings
        self.db = db
        self.http_client = http_client
        self.spotify_auth = spotify_auth
        self.spotify = spotify
        self.odesli = odesli
        self.oembed = oembed
        self.pipeline = pipeline
        self.start_time = discord.utils.utcnow()

    async def setup_hook(self) -> None:
        from cratebot.discordbot.cogs.commands import CommandsCog
        from cratebot.discordbot.cogs.context_menu import register_context_menu
        from cratebot.discordbot.cogs.monitoring import MonitoringCog

        await self.add_cog(MonitoringCog(self))
        await self.add_cog(CommandsCog(self))
        register_context_menu(self)

        if self.settings.discord_guild_id:
            guild = discord.Object(id=self.settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        logger.info("discord.commands_synced", count=len(synced))

    async def on_ready(self) -> None:
        logger.info("discord.ready", user=str(self.user), guilds=[g.id for g in self.guilds])
        if not self.settings.discord_guild_id:
            logger.warning("discord.no_guild_id_configured")

    def is_monitored_channel(self, channel: discord.abc.MessageableChannel) -> bool:
        channel_id = getattr(channel, "id", None)
        parent_id = getattr(channel, "parent_id", None)  # threads live under a monitored parent
        monitored = set(self.settings.monitored_channel_ids)
        return channel_id in monitored or (parent_id is not None and parent_id in monitored)

    def is_admin(self, member: discord.Member) -> bool:
        if member.guild_permissions.manage_messages:
            return True
        admin_roles = set(self.settings.admin_role_ids)
        return any(role.id in admin_roles for role in member.roles)
