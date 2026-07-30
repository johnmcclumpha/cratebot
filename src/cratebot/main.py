"""Entry point: wires config, DB, Spotify auth/client, resolver, pipeline,
and the Discord bot together, then runs the single long-running Gateway
connection (brief section 3's mechanism decision)."""

from __future__ import annotations

import asyncio

import httpx

from cratebot import __version__
from cratebot.config import get_settings
from cratebot.crypto import TokenCipher
from cratebot.db import Database
from cratebot.discordbot.bot import Cratebot
from cratebot.links.resolver import OdesliClient, YouTubeOEmbedClient
from cratebot.logging_setup import configure_logging, get_logger
from cratebot.pipeline import AddPipeline
from cratebot.spotify.auth import SpotifyAuth
from cratebot.spotify.client import SpotifyClient
from cratebot.spotify.errors import (
    PlaylistNotOwnedError,
    SpotifyAPIError,
    SpotifyAuthNotConfigured,
    SpotifyReauthRequired,
)

logger = get_logger(__name__)


async def _validate_playlist_ownership(settings, db: Database, spotify: SpotifyClient) -> None:
    """Refuse to silently limp along on a playlist the bot can't write to (brief 2.1)."""
    playlist_id = await db.get_config("playlist_id") or settings.spotify_playlist_id
    if not playlist_id:
        logger.warning("startup.no_playlist_configured", hint="Run /playlist set <url> in Discord.")
        return
    try:
        playlist = await spotify.get_playlist(playlist_id)
        me = await spotify.get_me()
    except SpotifyAuthNotConfigured:
        logger.warning("startup.spotify_not_authorised", hint="Run `cratebot-auth` on the host.")
        return
    except SpotifyReauthRequired:
        logger.warning("startup.spotify_reauth_required", hint="Run `cratebot-auth` on the host.")
        return
    except SpotifyAPIError as exc:
        logger.warning("startup.playlist_lookup_failed", error=exc.message)
        return

    owner_id = playlist.get("owner", {}).get("id", "")
    me_id = me.get("id", "")
    if owner_id != me_id:
        error = PlaylistNotOwnedError(playlist_id, owner_id, me_id)
        logger.error("startup.playlist_not_owned", playlist_id=playlist_id, owner_id=owner_id, me_id=me_id)
        raise error
    logger.info("startup.playlist_ok", name=playlist.get("name"), id=playlist_id)


async def _amain() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("startup.begin", version=__version__)

    db = Database(settings.database_path)
    await db.connect()

    http_client = httpx.AsyncClient(timeout=15.0, follow_redirects=False)
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())

    spotify_auth = SpotifyAuth(settings, db, cipher, http_client)
    spotify = SpotifyClient(http_client, spotify_auth.get_valid_access_token)
    odesli = OdesliClient(http_client, db, api_key=settings.odesli_api_key.get_secret_value())
    oembed = YouTubeOEmbedClient(http_client)
    pipeline = AddPipeline(settings, db, http_client, spotify, odesli, oembed)

    try:
        await _validate_playlist_ownership(settings, db, spotify)
    except PlaylistNotOwnedError as exc:
        logger.error("startup.refusing_misconfigured_playlist", error=str(exc))

    bot = Cratebot(
        settings=settings,
        db=db,
        http_client=http_client,
        spotify_auth=spotify_auth,
        spotify=spotify,
        odesli=odesli,
        oembed=oembed,
        pipeline=pipeline,
    )

    try:
        async with bot:
            await bot.start(settings.discord_bot_token.get_secret_value())
    finally:
        await http_client.aclose()
        await db.close()


def run() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    run()
