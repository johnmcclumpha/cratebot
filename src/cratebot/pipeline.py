"""End-to-end link -> Spotify-track-URI -> playlist-add orchestration.

Wires together the link parser, cross-platform resolver, matcher, Spotify
client, and the three dedupe layers from brief 5.6:

1. message-level: (message_id, link) pairs, so re-scans/edits are idempotent
2. track-level: Spotify track IDs the bot has already added, scoped per
   playlist (one bot instance can serve several Discord servers, each with
   its own playlist)
3. playlist-truth: reconcile against the live playlist so a human's manual
   removal is never silently undone (recorded in `suppressed_tracks`)

`playlist_id` is always passed in by the caller (resolved per-guild via
Cratebot.resolve_playlist_id) rather than held as pipeline state, since a
single pipeline instance serves every configured guild.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import httpx

from cratebot.config import Settings
from cratebot.db import Database
from cratebot.links.parser import ParsedLink, Platform, parse_link
from cratebot.links.resolver import OdesliClient, YouTubeOEmbedClient, resolve_short_link
from cratebot.matching import Candidate, best_match, rank_candidates
from cratebot.logging_setup import get_logger
from cratebot.spotify.client import SpotifyClient, extract_track_entry
from cratebot.spotify.errors import SpotifyAPIError

logger = get_logger(__name__)


class AddStatus(str, Enum):
    ADDED = "added"
    DRY_RUN = "dry_run"
    DUPLICATE = "duplicate"
    ALREADY_PROCESSED = "already_processed"
    AMBIGUOUS = "ambiguous"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class AddOutcome:
    status: AddStatus
    message: str
    track_id: str | None = None
    uri: str | None = None
    title: str | None = None
    artist: str | None = None
    candidates: list[tuple[Candidate, float]] = field(default_factory=list)
    source_url: str | None = None


class AddPipeline:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        http: httpx.AsyncClient,
        spotify: SpotifyClient,
        odesli: OdesliClient,
        oembed: YouTubeOEmbedClient,
    ) -> None:
        self._settings = settings
        self._db = db
        self._http = http
        self._spotify = spotify
        self._odesli = odesli
        self._oembed = oembed

    @property
    def db(self) -> Database:
        return self._db

    async def reconcile_playlist(self, playlist_id: str) -> dict[str, int]:
        """Detect tracks a human removed via the Spotify client, so we never silently re-add them."""
        remote_ids: set[str] = set()
        async for entry in self._spotify.iter_playlist_items(playlist_id):
            track = extract_track_entry(entry)
            if track and track.get("id"):
                remote_ids.add(track["id"])

        local_ids = await self._db.get_added_track_ids(playlist_id)
        missing = local_ids - remote_ids
        for track_id in missing:
            await self._db.suppress_track(playlist_id, track_id, reason="removed_by_human")
            await self._db.remove_added_track(playlist_id, track_id)

        logger.info("pipeline.reconciled", remote_total=len(remote_ids), newly_suppressed=len(missing))
        return {"remote_total": len(remote_ids), "newly_suppressed": len(missing)}

    async def process_link(
        self,
        parsed: ParsedLink,
        *,
        playlist_id: str,
        message_id: str,
        requester_id: str | None,
        dry_run: bool = False,
    ) -> AddOutcome:
        if await self._db.is_link_processed(message_id, parsed.normalized_url):
            # Not a real repost - just this exact message being re-scanned (e.g. Discord
            # attaching its own link-preview embed fires an on_message_edit for content
            # we already handled). Distinct from AddStatus.DUPLICATE, which means a
            # *different* message linked a track that's already in the playlist.
            return AddOutcome(AddStatus.ALREADY_PROCESSED, "Already processed this link on this message.")

        if parsed.platform is Platform.SPOTIFY:
            outcome = await self._process_spotify_link(
                parsed, playlist_id=playlist_id, dry_run=dry_run, message_id=message_id, requester_id=requester_id
            )
        else:
            outcome = await self._process_foreign_link(parsed, playlist_id=playlist_id, dry_run=dry_run)

        if outcome.status is AddStatus.ADDED:
            await self._db.mark_link_processed(message_id, parsed.normalized_url)
            if outcome.track_id and outcome.uri:
                await self._db.record_added_track(
                    playlist_id, outcome.track_id, outcome.uri, outcome.title, outcome.artist, message_id, requester_id
                )
        elif outcome.status in (AddStatus.SKIPPED, AddStatus.DUPLICATE, AddStatus.AMBIGUOUS):
            # AMBIGUOUS also gets marked processed so the same embed-attach re-scan
            # doesn't re-post a second disambiguation prompt for this message.
            await self._db.mark_link_processed(message_id, parsed.normalized_url)

        return outcome

    async def confirm_candidate(
        self,
        candidate: Candidate,
        *,
        playlist_id: str,
        normalized_url: str,
        message_id: str,
        requester_id: str | None,
    ) -> AddOutcome:
        """Used by the disambiguation button UI once a human picks a candidate track."""
        outcome = await self._add_track_by_id(candidate.track_id, playlist_id=playlist_id, dry_run=False)
        if outcome.status is AddStatus.ADDED:
            await self._db.mark_link_processed(message_id, normalized_url)
            await self._db.record_added_track(
                playlist_id, outcome.track_id, outcome.uri, outcome.title, outcome.artist, message_id, requester_id
            )
        return outcome

    async def _process_spotify_link(
        self,
        parsed: ParsedLink,
        *,
        playlist_id: str,
        dry_run: bool,
        message_id: str,
        requester_id: str | None,
    ) -> AddOutcome:
        if parsed.is_short_link:
            resolved_url = await resolve_short_link(self._http, self._db, parsed.normalized_url)
            if resolved_url is None:
                return AddOutcome(AddStatus.FAILED, "Could not resolve spotify.link short link.")
            reparsed = parse_link(resolved_url)
            if reparsed is None:
                return AddOutcome(AddStatus.FAILED, "Resolved short link but couldn't classify it.")
            parsed = reparsed

        if parsed.spotify_type == "track":
            assert parsed.spotify_id is not None
            return await self._add_track_by_id(parsed.spotify_id, playlist_id=playlist_id, dry_run=dry_run)

        if parsed.spotify_type == "album":
            if not self._settings.expand_albums:
                return AddOutcome(AddStatus.SKIPPED, "Album links are not expanded (EXPAND_ALBUMS=false).")
            return await self._add_album(
                parsed.spotify_id,
                playlist_id=playlist_id,
                dry_run=dry_run,
                message_id=message_id,
                requester_id=requester_id,
            )

        if parsed.spotify_type == "playlist":
            return AddOutcome(AddStatus.SKIPPED, "Playlist links are never auto-expanded.")

        if parsed.spotify_type in ("episode", "show"):
            if not self._settings.expand_episodes:
                return AddOutcome(AddStatus.SKIPPED, "Episode/podcast links are skipped (EXPAND_EPISODES=false).")
            assert parsed.spotify_id is not None
            return await self._add_episode_by_id(parsed.spotify_id, playlist_id=playlist_id, dry_run=dry_run)

        return AddOutcome(AddStatus.FAILED, "Unrecognised Spotify link type.")

    async def _add_track_by_id(self, track_id: str, *, playlist_id: str, dry_run: bool) -> AddOutcome:
        if await self._db.is_suppressed(playlist_id, track_id):
            return AddOutcome(AddStatus.SKIPPED, "Previously removed by a human; not re-adding without override.")
        if await self._db.is_track_added(playlist_id, track_id):
            return AddOutcome(AddStatus.DUPLICATE, "Track already in the playlist (per local record).")

        try:
            track = await self._spotify.get_track(track_id)
        except SpotifyAPIError as exc:
            return AddOutcome(AddStatus.FAILED, f"Spotify lookup failed: {exc.message}")

        title = track.get("name")
        artist = ", ".join(a.get("name", "") for a in track.get("artists", []))
        uri = track.get("uri", f"spotify:track:{track_id}")

        if dry_run:
            return AddOutcome(AddStatus.DRY_RUN, f"Would add: {title} - {artist}", track_id, uri, title, artist)

        try:
            await self._spotify.add_items(playlist_id, [uri])
        except SpotifyAPIError as exc:
            return AddOutcome(AddStatus.FAILED, f"Add to playlist failed: {exc.message}")

        return AddOutcome(AddStatus.ADDED, f"Added: {title} - {artist}", track_id, uri, title, artist)

    async def _add_album(
        self,
        album_id: str | None,
        *,
        playlist_id: str,
        dry_run: bool,
        message_id: str,
        requester_id: str | None,
    ) -> AddOutcome:
        if album_id is None:
            return AddOutcome(AddStatus.FAILED, "Missing album ID.")
        try:
            tracks = await self._spotify.get_album_tracks(album_id)
        except SpotifyAPIError as exc:
            return AddOutcome(AddStatus.FAILED, f"Album lookup failed: {exc.message}")

        # Resolve which tracks are actually new first, then add them in one batched
        # request (spotify.add_items batches up to 100 URIs per call) instead of one
        # POST per track - an album expansion used to cost N sequential add calls.
        to_add: list[tuple[str, str, str | None, str | None]] = []
        seen_in_album: set[str] = set()
        for track in tracks:
            track_id = track.get("id")
            if not track_id or track_id in seen_in_album:
                continue
            seen_in_album.add(track_id)
            if await self._db.is_suppressed(playlist_id, track_id) or await self._db.is_track_added(
                playlist_id, track_id
            ):
                continue
            title = track.get("name")
            artist = ", ".join(a.get("name", "") for a in track.get("artists", []))
            uri = track.get("uri", f"spotify:track:{track_id}")
            to_add.append((track_id, uri, title, artist))

        if not to_add:
            return AddOutcome(AddStatus.SKIPPED, "Album expanded: 0 new tracks (already added or suppressed).")

        if dry_run:
            return AddOutcome(AddStatus.DRY_RUN, f"Album expanded: would add {len(to_add)} track(s).")

        try:
            await self._spotify.add_items(playlist_id, [uri for _, uri, _, _ in to_add])
        except SpotifyAPIError as exc:
            return AddOutcome(AddStatus.FAILED, f"Album add failed: {exc.message}")

        # _add_track_by_id normally leaves recording to process_link's single-track
        # outcome, but a multi-track album outcome can't carry N track_ids through
        # that path - record each one here instead, or a re-post of the same album
        # (or one of its tracks individually) would never be recognised as a duplicate.
        for track_id, uri, title, artist in to_add:
            await self._db.record_added_track(playlist_id, track_id, uri, title, artist, message_id, requester_id)

        return AddOutcome(AddStatus.ADDED, f"Album expanded: {len(to_add)} track(s) added.")

    async def _add_episode_by_id(self, episode_id: str, *, playlist_id: str, dry_run: bool) -> AddOutcome:
        # Episode metadata lookup isn't wired up (out of scope for v1); add the URI best-effort.
        uri = f"spotify:episode:{episode_id}"
        if await self._db.is_suppressed(playlist_id, episode_id) or await self._db.is_track_added(
            playlist_id, episode_id
        ):
            return AddOutcome(AddStatus.DUPLICATE, "Episode already added or suppressed.")
        if dry_run:
            return AddOutcome(AddStatus.DRY_RUN, "Would add episode.", episode_id, uri)
        try:
            await self._spotify.add_items(playlist_id, [uri])
        except SpotifyAPIError as exc:
            return AddOutcome(AddStatus.FAILED, f"Add episode failed: {exc.message}")
        return AddOutcome(AddStatus.ADDED, "Added episode.", episode_id, uri)

    async def _process_foreign_link(self, parsed: ParsedLink, *, playlist_id: str, dry_run: bool) -> AddOutcome:
        odesli_result = await self._odesli.resolve(parsed.normalized_url)
        if odesli_result is not None:
            return await self._add_track_by_id(odesli_result.spotify_track_id, playlist_id=playlist_id, dry_run=dry_run)

        if parsed.platform is Platform.YOUTUBE:
            title_artist = await self._oembed.get_title_artist(parsed.normalized_url)
            if title_artist is None:
                return AddOutcome(AddStatus.FAILED, "Could not resolve title from YouTube oEmbed.")
            title, artist = title_artist
            query = f"{title} {artist}".strip()
            try:
                raw_candidates = await self._spotify.search_tracks_paginated(query, max_candidates=10)
            except SpotifyAPIError as exc:
                return AddOutcome(AddStatus.FAILED, f"Spotify search failed: {exc.message}")

            candidates = [_candidate_from_track(t) for t in raw_candidates]
            match = best_match(title, artist, candidates, threshold=self._settings.match_threshold)
            if match is not None:
                candidate, _score = match
                return await self._add_track_by_id(candidate.track_id, playlist_id=playlist_id, dry_run=dry_run)

            ranked = rank_candidates(title, artist, candidates)[:3]
            return AddOutcome(
                AddStatus.AMBIGUOUS,
                f"No confident match for '{title}' by '{artist}'. Pick one of the top candidates.",
                candidates=ranked,
                source_url=parsed.normalized_url,
            )

        return AddOutcome(AddStatus.FAILED, f"No resolver available for platform '{parsed.platform.value}'.")


def _candidate_from_track(track: dict) -> Candidate:
    return Candidate(
        track_id=track["id"],
        uri=track.get("uri", f"spotify:track:{track['id']}"),
        title=track.get("name", ""),
        artist=", ".join(a.get("name", "") for a in track.get("artists", [])),
        isrc=track.get("external_ids", {}).get("isrc"),
    )
