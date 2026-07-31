from __future__ import annotations

import httpx
import pytest_asyncio
import respx

from cratebot.config import Settings
from cratebot.db import Database
from cratebot.links.parser import parse_link
from cratebot.links.resolver import ODESLI_URL, OdesliClient, YouTubeOEmbedClient
from cratebot.pipeline import AddPipeline, AddStatus

TRACK_URL = "https://open.spotify.com/track/abc123"
ALBUM_URL = "https://open.spotify.com/album/album123"
YOUTUBE_URL = "https://youtu.be/abc123"


@pytest_asyncio.fixture
async def pipeline(db: Database):
    async with httpx.AsyncClient() as http:
        settings = Settings(spotify_playlist_id="playlist123", _env_file=None)
        odesli = OdesliClient(http, db)
        oembed = YouTubeOEmbedClient(http)
        yield AddPipeline(settings, db, http, _StubSpotify(), odesli, oembed)


def _pipeline_with(db: Database, http: httpx.AsyncClient, spotify, **settings_kwargs) -> AddPipeline:
    settings = Settings(spotify_playlist_id="playlist123", _env_file=None, **settings_kwargs)
    odesli = OdesliClient(http, db)
    oembed = YouTubeOEmbedClient(http)
    return AddPipeline(settings, db, http, spotify, odesli, oembed)


class _StubSpotify:
    def __init__(self, album_tracks: list[dict] | None = None, search_results: list[dict] | None = None) -> None:
        self.add_items_calls: list[tuple[str, list[str]]] = []
        self._album_tracks = album_tracks or []
        self._search_results = search_results or []

    async def get_track(self, track_id: str) -> dict:
        return {"id": track_id, "name": "Song", "artists": [{"name": "Artist"}], "uri": f"spotify:track:{track_id}"}

    async def add_items(self, playlist_id: str, uris: list[str]) -> None:
        self.add_items_calls.append((playlist_id, list(uris)))

    async def get_album_tracks(self, album_id: str) -> list[dict]:
        return self._album_tracks

    async def search_tracks_paginated(self, query: str, max_candidates: int = 10) -> list[dict]:
        return self._search_results


@respx.mock
async def test_same_message_reprocessed_is_not_flagged_duplicate(pipeline: AddPipeline) -> None:
    """Discord attaching its own link-preview embed re-fires on_message_edit for a
    message we already handled. Re-running process_link for the *same* message_id
    and link must not come back as AddStatus.DUPLICATE (that's reserved for a
    genuinely different message reposting an already-added track) - otherwise the
    bot shows a spurious duplicate reaction alongside the added reaction."""
    parsed = parse_link(TRACK_URL)
    assert parsed is not None

    first = await pipeline.process_link(parsed, message_id="msg1", requester_id="user1")
    assert first.status is AddStatus.ADDED

    second = await pipeline.process_link(parsed, message_id="msg1", requester_id="user1")
    assert second.status is AddStatus.ALREADY_PROCESSED


@respx.mock
async def test_different_message_same_track_is_duplicate(pipeline: AddPipeline) -> None:
    """A different message linking a track that's already in the playlist should
    still be flagged as a real duplicate."""
    parsed = parse_link(TRACK_URL)
    assert parsed is not None

    first = await pipeline.process_link(parsed, message_id="msg1", requester_id="user1")
    assert first.status is AddStatus.ADDED

    second = await pipeline.process_link(parsed, message_id="msg2", requester_id="user2")
    assert second.status is AddStatus.DUPLICATE


# -- album expansion ------------------------------------------------------

_ALBUM_TRACKS = [
    {"id": "t1", "name": "Track One", "artists": [{"name": "Artist"}], "uri": "spotify:track:t1"},
    {"id": "t2", "name": "Track Two", "artists": [{"name": "Artist"}], "uri": "spotify:track:t2"},
]


async def test_add_album_batches_into_one_add_items_call(db: Database) -> None:
    """Album expansion used to POST one add_items call per track; it should now
    batch every new track into a single call."""
    async with httpx.AsyncClient() as http:
        spotify = _StubSpotify(album_tracks=_ALBUM_TRACKS)
        pipeline = _pipeline_with(db, http, spotify, expand_albums=True)
        parsed = parse_link(ALBUM_URL)
        assert parsed is not None
        outcome = await pipeline.process_link(parsed, message_id="msg1", requester_id="user1")

    assert outcome.status is AddStatus.ADDED
    assert len(spotify.add_items_calls) == 1
    playlist_id, uris = spotify.add_items_calls[0]
    assert playlist_id == "playlist123"
    assert set(uris) == {"spotify:track:t1", "spotify:track:t2"}
    assert await db.is_track_added("t1")
    assert await db.is_track_added("t2")


async def test_add_album_skips_already_added_tracks(db: Database) -> None:
    """A track already added (from an earlier post of it individually, or a
    previous album expansion) must not be re-added or re-counted."""
    await db.record_added_track("t1", "spotify:track:t1", "Track One", "Artist", "prev-msg", "prev-user")
    async with httpx.AsyncClient() as http:
        spotify = _StubSpotify(album_tracks=_ALBUM_TRACKS)
        pipeline = _pipeline_with(db, http, spotify, expand_albums=True)
        parsed = parse_link(ALBUM_URL)
        assert parsed is not None
        outcome = await pipeline.process_link(parsed, message_id="msg1", requester_id="user1")

    assert outcome.status is AddStatus.ADDED
    assert len(spotify.add_items_calls) == 1
    assert spotify.add_items_calls[0][1] == ["spotify:track:t2"]


async def test_add_album_disabled_by_default_is_skipped(db: Database) -> None:
    async with httpx.AsyncClient() as http:
        spotify = _StubSpotify(album_tracks=_ALBUM_TRACKS)
        pipeline = _pipeline_with(db, http, spotify)  # expand_albums defaults False
        parsed = parse_link(ALBUM_URL)
        assert parsed is not None
        outcome = await pipeline.process_link(parsed, message_id="msg1", requester_id="user1")

    assert outcome.status is AddStatus.SKIPPED
    assert spotify.add_items_calls == []


# -- episode expansion -----------------------------------------------------

EPISODE_URL = "https://open.spotify.com/episode/ep123"


async def test_add_episode_dry_run_does_not_call_spotify(db: Database) -> None:
    async with httpx.AsyncClient() as http:
        spotify = _StubSpotify()
        pipeline = _pipeline_with(db, http, spotify, expand_episodes=True)
        parsed = parse_link(EPISODE_URL)
        assert parsed is not None
        outcome = await pipeline.process_link(parsed, message_id="msg1", requester_id="user1", dry_run=True)

    assert outcome.status is AddStatus.DRY_RUN
    assert spotify.add_items_calls == []


async def test_add_episode_live_adds_and_records_locally(db: Database) -> None:
    async with httpx.AsyncClient() as http:
        spotify = _StubSpotify()
        pipeline = _pipeline_with(db, http, spotify, expand_episodes=True)
        parsed = parse_link(EPISODE_URL)
        assert parsed is not None
        outcome = await pipeline.process_link(parsed, message_id="msg1", requester_id="user1")

    assert outcome.status is AddStatus.ADDED
    assert spotify.add_items_calls == [("playlist123", ["spotify:episode:ep123"])]
    assert await db.is_track_added("ep123")


async def test_episode_expansion_disabled_by_default_is_skipped(db: Database) -> None:
    async with httpx.AsyncClient() as http:
        spotify = _StubSpotify()
        pipeline = _pipeline_with(db, http, spotify)  # expand_episodes defaults False
        parsed = parse_link(EPISODE_URL)
        assert parsed is not None
        outcome = await pipeline.process_link(parsed, message_id="msg1", requester_id="user1")

    assert outcome.status is AddStatus.SKIPPED


# -- foreign (non-Spotify) link resolution ----------------------------------


@respx.mock
async def test_foreign_link_confident_match_is_added(db: Database) -> None:
    """Odesli misses, so it falls back to YouTube oEmbed + Spotify search; a
    high-confidence fuzzy match should be added without asking a human."""
    respx.get(ODESLI_URL).mock(return_value=httpx.Response(404))
    respx.get("https://www.youtube.com/oembed").mock(
        return_value=httpx.Response(200, json={"title": "Cool Song", "author_name": "Cool Artist"})
    )
    async with httpx.AsyncClient() as http:
        spotify = _StubSpotify(
            search_results=[
                {
                    "id": "match1",
                    "name": "Cool Song",
                    "artists": [{"name": "Cool Artist"}],
                    "uri": "spotify:track:match1",
                }
            ]
        )
        pipeline = _pipeline_with(db, http, spotify)
        parsed = parse_link(YOUTUBE_URL)
        assert parsed is not None
        outcome = await pipeline.process_link(parsed, message_id="msg1", requester_id="user1")

    assert outcome.status is AddStatus.ADDED


@respx.mock
async def test_foreign_link_no_confident_match_is_ambiguous(db: Database) -> None:
    """A poor fuzzy match must not be auto-added - ask a human instead."""
    respx.get(ODESLI_URL).mock(return_value=httpx.Response(404))
    respx.get("https://www.youtube.com/oembed").mock(
        return_value=httpx.Response(200, json={"title": "Totally Different Song", "author_name": "Some Artist"})
    )
    async with httpx.AsyncClient() as http:
        spotify = _StubSpotify(
            search_results=[
                {
                    "id": "no-match",
                    "name": "Nothing Alike Whatsoever",
                    "artists": [{"name": "Unrelated Band"}],
                    "uri": "spotify:track:no-match",
                }
            ]
        )
        pipeline = _pipeline_with(db, http, spotify)
        parsed = parse_link(YOUTUBE_URL)
        assert parsed is not None
        outcome = await pipeline.process_link(parsed, message_id="msg1", requester_id="user1")

    assert outcome.status is AddStatus.AMBIGUOUS
    assert outcome.candidates


@respx.mock
async def test_real_youtube_watch_url_resolves_dry_run_only(db: Database) -> None:
    """Regression fixture for the query-stripping bug: a real, permanently-stable
    youtube.com/watch?v= URL (not a synthetic placeholder) must survive
    normalization with its video ID intact and resolve to a confident Spotify
    match. Odesli/oEmbed are mocked - no live network calls - and this runs
    dry_run=True, so nothing is written to the database or to Spotify."""
    real_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    real_track_id = "4PTG3Z6ehGkBFwjybzWkR8"  # verified via a live, read-only Spotify search

    respx.get(ODESLI_URL).mock(return_value=httpx.Response(404))
    respx.get("https://www.youtube.com/oembed").mock(
        return_value=httpx.Response(
            200,
            json={
                "title": "Rick Astley - Never Gonna Give You Up (Official Music Video)",
                "author_name": "Rick Astley",
            },
        )
    )
    async with httpx.AsyncClient() as http:
        spotify = _StubSpotify(
            search_results=[
                {
                    "id": real_track_id,
                    "name": "Never Gonna Give You Up",
                    "artists": [{"name": "Rick Astley"}],
                    "uri": f"spotify:track:{real_track_id}",
                }
            ]
        )
        pipeline = _pipeline_with(db, http, spotify)

        parsed = parse_link(real_url)
        assert parsed is not None
        # the fix: the video ID must survive normalization, not collapse to a bare /watch
        assert parsed.normalized_url == real_url

        outcome = await pipeline.process_link(
            parsed, message_id="rickroll-test", requester_id="user1", dry_run=True
        )

    assert outcome.status is AddStatus.DRY_RUN
    assert outcome.track_id == real_track_id

    # dry_run must not touch Spotify or the local database at all
    assert spotify.add_items_calls == []
    assert not await db.is_track_added(real_track_id)
    assert not await db.is_link_processed("rickroll-test", real_url)
