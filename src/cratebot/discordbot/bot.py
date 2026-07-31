"""The bot's main Client subclass: one Gateway connection handling both
passive monitoring (MESSAGE_CREATE) and slash/context-menu commands, per
the mechanism decision in brief section 3. One process serves every
configured guild - GuildConfig lookup and per-guild playlist resolution
live here since Cratebot already owns guild-scoped channel/admin checks.
"""

from __future__ import annotations

import discord
import httpx
from discord.ext import commands

from cratebot.config import GuildConfig, Settings
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
        self._guild_configs: dict[int, GuildConfig] = {g.guild_id: g for g in settings.guilds}
        self._playlist_id_cache: dict[int, str] = {}

    async def setup_hook(self) -> None:
        from cratebot.discordbot.cogs.commands import CommandsCog
        from cratebot.discordbot.cogs.context_menu import register_context_menu
        from cratebot.discordbot.cogs.monitoring import MonitoringCog

        await self.add_cog(MonitoringCog(self))
        await self.add_cog(CommandsCog(self))
        register_context_menu(self)

        if self.settings.guilds:
            synced_total = 0
            for guild_cfg in self.settings.guilds:
                guild = discord.Object(id=guild_cfg.guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                synced_total += len(synced)
            logger.info("discord.commands_synced", count=synced_total, guild_count=len(self.settings.guilds))
        else:
            synced = await self.tree.sync()
            logger.info("discord.commands_synced", count=len(synced))

    async def on_ready(self) -> None:
        logger.info("discord.ready", user=str(self.user), guilds=[g.id for g in self.guilds])
        if not self.settings.guilds:
            logger.warning("discord.no_guilds_configured")

    def guild_config(self, guild_id: int) -> GuildConfig | None:
        return self._guild_configs.get(guild_id)

    async def resolve_playlist_id(self, guild_id: int) -> str | None:
        """Effective playlist for a guild: a /playlist set override if one's been
        recorded, else the GUILDS-configured default. Cached per guild - avoids a
        DB round trip on every single link processed."""
        if guild_id in self._playlist_id_cache:
            return self._playlist_id_cache[guild_id]
        guild_cfg = self._guild_configs.get(guild_id)
        if guild_cfg is None:
            return None
        override = await self.db.get_config(f"playlist_id:{guild_id}")
        playlist_id = override or guild_cfg.spotify_playlist_id
        self._playlist_id_cache[guild_id] = playlist_id
        return playlist_id

    def invalidate_playlist_id_cache(self, guild_id: int) -> None:
        self._playlist_id_cache.pop(guild_id, None)

    def is_monitored_channel(self, channel: discord.abc.MessageableChannel) -> GuildConfig | None:
        guild = getattr(channel, "guild", None)
        guild_cfg = self._guild_configs.get(guild.id) if guild is not None else None
        if guild_cfg is None:
            return None
        channel_id = getattr(channel, "id", None)
        parent_id = getattr(channel, "parent_id", None)  # threads live under a monitored parent
        monitored = set(guild_cfg.monitored_channel_ids)
        if channel_id in monitored or (parent_id is not None and parent_id in monitored):
            return guild_cfg
        return None

    def is_admin(self, member: discord.Member) -> bool:
        if member.guild_permissions.manage_messages:
            return True
        guild_cfg = self._guild_configs.get(member.guild.id)
        if guild_cfg is None:
            return False
        admin_roles = set(guild_cfg.admin_role_ids)
        return any(role.id in admin_roles for role in member.roles)
