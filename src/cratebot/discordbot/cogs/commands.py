"""Slash commands: /status, /scan, /playlist set.

The interaction-lifecycle rules from brief 5.4 matter here: a slash command
must be acknowledged within 3 seconds (`defer()`), and the 15-minute
interaction token window will not cover a large backfill - once it's gone,
progress/final messages fall back to a normal channel message instead of
trying (and failing) to edit the interaction response.
"""

from __future__ import annotations

import time
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from cratebot.links.parser import parse_link
from cratebot.logging_setup import get_logger
from cratebot.scan import ScanProgress, SearchUnavailableError, run_history_scan, run_search_scan
from cratebot.spotify.client import playlist_total
from cratebot.spotify.errors import (
    PlaylistNotOwnedError,
    SpotifyAPIError,
    SpotifyAuthNotConfigured,
    SpotifyReauthRequired,
)

logger = get_logger(__name__)

INTERACTION_TOKEN_LIFETIME = 15 * 60
PROGRESS_EDIT_INTERVAL = 5.0
PLAYLIST_WARN_THRESHOLD = 9500  # brief 5.5: cap is 10,000 items, verify at build time


class ScanLimitReached(Exception):
    pass


def _format_progress(progress: ScanProgress, dry_run: bool, *, final: bool = False) -> str:
    header = "Scan complete" if final else "Scanning..."
    mode = "(dry run)" if dry_run else "(live)"
    lines = [f"**{header}** {mode} - strategy: {progress.strategy}"]
    lines.append(
        f"Seen: {progress.total_seen} | Added: {progress.added} | Duplicates: {progress.duplicates} | "
        f"Ambiguous: {progress.ambiguous} | Failed: {progress.failed} | Skipped: {progress.skipped}"
    )
    if progress.still_indexing:
        lines.append("Guild search index isn't ready yet; retrying automatically.")
    for note in progress.notes[-3:]:
        lines.append(f"note: {note}")
    return "\n".join(lines)


class CommandsCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        user = interaction.user
        return isinstance(user, discord.Member) and self.bot.is_admin(user)

    @app_commands.command(name="status", description="Show Cratebot's playlist, token, and scan status.")
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        settings = self.bot.settings
        lines: list[str] = []

        try:
            playlist_id = await self.bot.pipeline._playlist_id()
            playlist = await self.bot.spotify.get_playlist(playlist_id)
            total = playlist_total(playlist)
            lines.append(f"**Playlist:** {playlist.get('name')} - {total} tracks")
            lines.append(f"https://open.spotify.com/playlist/{playlist_id}")
            if total >= PLAYLIST_WARN_THRESHOLD:
                lines.append(f"Approaching the playlist cap ({total}/10000).")
        except SpotifyAuthNotConfigured:
            lines.append("Spotify not authorised yet. Run `cratebot-auth` on the host, then restart the bot.")
        except SpotifyReauthRequired:
            lines.append("Spotify re-authorisation needed. Run `cratebot-auth` on the host, then restart.")
        except RuntimeError as exc:
            lines.append(str(exc))
        except SpotifyAPIError as exc:
            lines.append(f"Spotify error: {exc.message}")

        added_count = await self.bot.db.count_added_tracks()
        lines.append(f"**Tracks added by bot (local record):** {added_count}")

        for channel_id in settings.monitored_channel_ids:
            cursor = await self.bot.db.get_cursor(str(channel_id))
            if cursor:
                lines.append(f"Scan cursor for <#{channel_id}>: message {cursor}")

        lines.append(f"**Match threshold:** {settings.match_threshold}")
        lines.append(f"**Scan strategy:** {settings.scan_strategy}")
        lines.append(f"**Dry-run default:** {settings.dry_run_default}")

        await interaction.followup.send("\n".join(lines))

    playlist_group = app_commands.Group(name="playlist", description="Manage the target Spotify playlist")

    @playlist_group.command(name="set", description="Set the target playlist (must be owned by the bot's Spotify account).")
    @app_commands.describe(url="Spotify playlist URL or URI")
    async def playlist_set(self, interaction: discord.Interaction, url: str) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "You need Manage Messages (or an admin role) to do that.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        parsed = parse_link(url)
        if parsed is None or parsed.spotify_type != "playlist" or not parsed.spotify_id:
            await interaction.followup.send("That doesn't look like a Spotify playlist URL.")
            return

        try:
            playlist = await self.bot.spotify.get_playlist(parsed.spotify_id)
            me = await self.bot.spotify.get_me()
        except SpotifyAuthNotConfigured:
            await interaction.followup.send("Spotify isn't authorised yet. Run `cratebot-auth` on the host first.")
            return
        except SpotifyAPIError as exc:
            await interaction.followup.send(f"Couldn't look up that playlist: {exc.message}")
            return

        owner_id = playlist.get("owner", {}).get("id", "")
        me_id = me.get("id", "")
        if owner_id != me_id:
            await interaction.followup.send(str(PlaylistNotOwnedError(parsed.spotify_id, owner_id, me_id)))
            return

        await self.bot.db.set_config("playlist_id", parsed.spotify_id)
        await interaction.followup.send(
            f"Playlist set to **{playlist.get('name')}** ({playlist_total(playlist)} tracks)."
        )

    @app_commands.command(name="scan", description="Backfill: scan channel history for music links.")
    @app_commands.describe(
        days="How many days back to scan",
        since_message_id="Resume/start from this message ID (snowflake), overrides `days`",
        dry_run="Preview only, don't actually add anything (default: true)",
        channel="Channel to scan (default: this channel)",
        limit="Stop after seeing this many matching messages",
    )
    async def scan(
        self,
        interaction: discord.Interaction,
        days: int | None = None,
        since_message_id: str | None = None,
        dry_run: bool = True,
        channel: discord.TextChannel | None = None,
        limit: int | None = None,
    ) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "You need Manage Messages (or an admin role) to run /scan.", ephemeral=True
            )
            return

        target_channel = channel or interaction.channel
        if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Pick a text channel or thread.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        min_id: int | None = None
        if since_message_id:
            min_id = int(since_message_id)
        elif days:
            min_id = discord.utils.time_snowflake(discord.utils.utcnow() - timedelta(days=days))

        started_at = time.monotonic()
        state = {"last_edit": 0.0, "token_expired": False}

        async def progress_cb(progress: ScanProgress) -> None:
            if limit is not None and progress.total_seen >= limit:
                raise ScanLimitReached()
            now = time.monotonic()
            if now - state["last_edit"] < PROGRESS_EDIT_INTERVAL:
                return
            state["last_edit"] = now
            if now - started_at >= INTERACTION_TOKEN_LIFETIME - 30:
                state["token_expired"] = True
                return
            try:
                await interaction.edit_original_response(content=_format_progress(progress, dry_run))
            except discord.HTTPException:
                state["token_expired"] = True

        progress: ScanProgress | None = None
        try:
            strategy = self.bot.settings.scan_strategy
            if strategy == "search":
                try:
                    progress = await run_search_scan(
                        self.bot,
                        self.bot.pipeline,
                        self.bot.settings.discord_guild_id,
                        [target_channel.id],
                        min_id=min_id,
                        dry_run=dry_run,
                        progress_cb=progress_cb,
                    )
                except SearchUnavailableError:
                    await target_channel.send("Search index unavailable; falling back to a full history walk.")
                    progress = await run_history_scan(
                        self.bot,
                        self.bot.pipeline,
                        target_channel,
                        resume=since_message_id is None,
                        after_id=min_id,
                        dry_run=dry_run,
                        progress_cb=progress_cb,
                        get_cursor=self.bot.db.get_cursor,
                        set_cursor=self.bot.db.set_cursor,
                    )
            else:
                progress = await run_history_scan(
                    self.bot,
                    self.bot.pipeline,
                    target_channel,
                    resume=since_message_id is None,
                    after_id=min_id,
                    dry_run=dry_run,
                    progress_cb=progress_cb,
                    get_cursor=self.bot.db.get_cursor,
                    set_cursor=self.bot.db.set_cursor,
                )
        except ScanLimitReached:
            pass
        except Exception:
            logger.exception("scan.failed")
            await target_channel.send("Scan failed with an internal error; check the logs.")
            return

        if progress is None:
            await target_channel.send("Scan stopped before collecting any results.")
            return

        summary = _format_progress(progress, dry_run, final=True)
        if state["token_expired"]:
            await target_channel.send(summary)
        else:
            try:
                await interaction.edit_original_response(content=summary)
            except discord.HTTPException:
                await target_channel.send(summary)
