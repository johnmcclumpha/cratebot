from __future__ import annotations

import httpx
import pytest_asyncio
import respx

from cratebot.config import Settings
from cratebot.db import Database
from cratebot.links.parser import parse_link
from cratebot.links.resolver import OdesliClient, YouTubeOEmbedClient
from cratebot.pipeline import AddPipeline, AddStatus

TRACK_URL = "https://open.spotify.com/track/abc123"


@pytest_asyncio.fixture
async def pipeline(db: Database):
    async with httpx.AsyncClient() as http:
        settings = Settings(spotify_playlist_id="playlist123", _env_file=None)
        odesli = OdesliClient(http, db)
        oembed = YouTubeOEmbedClient(http)
        yield AddPipeline(settings, db, http, _StubSpotify(), odesli, oembed)


class _StubSpotify:
    async def get_track(self, track_id: str) -> dict:
        return {"id": track_id, "name": "Song", "artists": [{"name": "Artist"}], "uri": f"spotify:track:{track_id}"}

    async def add_items(self, playlist_id: str, uris: list[str]) -> None:
        return None


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
