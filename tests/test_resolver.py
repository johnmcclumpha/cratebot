from __future__ import annotations

import httpx
import pytest
import respx

from cratebot.db import Database
from cratebot.links.resolver import ODESLI_URL, OdesliClient, YouTubeOEmbedClient, clean_title, resolve_short_link



@respx.mock
async def test_odesli_resolves_and_caches(db: Database) -> None:
    route = respx.get(ODESLI_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "linksByPlatform": {
                    "spotify": {
                        "url": "https://open.spotify.com/track/abc123",
                        "entityUniqueId": "SPOTIFY_SONG::abc123",
                    }
                },
                "entitiesByUniqueId": {
                    "SPOTIFY_SONG::abc123": {"title": "Song Title", "artistName": "Some Artist"}
                },
            },
        )
    )
    async with httpx.AsyncClient() as http:
        client = OdesliClient(http, db, rate_per_minute=600)
        result = await client.resolve("https://youtu.be/xyz")
        assert result is not None
        assert result.spotify_track_id == "abc123"
        assert result.title == "Song Title"

        # second call must hit the cache, not the network
        result2 = await client.resolve("https://youtu.be/xyz")
        assert result2 is not None
        assert result2.spotify_track_id == "abc123"

    assert route.call_count == 1


@respx.mock
async def test_odesli_404_cached_as_permanent_miss(db: Database) -> None:
    route = respx.get(ODESLI_URL).mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as http:
        client = OdesliClient(http, db, rate_per_minute=600)
        result = await client.resolve("https://youtu.be/nomatch")
        assert result is None
        # cached as a miss - a second call should not hit the network again
        result2 = await client.resolve("https://youtu.be/nomatch")
        assert result2 is None
    assert route.call_count == 1


@respx.mock
async def test_odesli_timeout_is_not_cached_and_returns_none(db: Database) -> None:
    respx.get(ODESLI_URL).mock(side_effect=httpx.TimeoutException("timed out"))
    async with httpx.AsyncClient() as http:
        client = OdesliClient(http, db, rate_per_minute=600)
        result = await client.resolve("https://youtu.be/slow")
    assert result is None
    cached = await db.get_odesli_cache("https://youtu.be/slow")
    assert cached is None  # transient failure, not a permanent negative cache


@respx.mock
async def test_odesli_circuit_breaker_opens_after_repeated_failures(db: Database) -> None:
    respx.get(ODESLI_URL).mock(side_effect=httpx.TimeoutException("timed out"))
    async with httpx.AsyncClient() as http:
        client = OdesliClient(http, db, rate_per_minute=6000)
        for i in range(5):
            await client.resolve(f"https://youtu.be/fail{i}")
        assert client._circuit.is_open
        # while open, resolve() must short-circuit without calling the network
        calls_before = respx.calls.call_count
        result = await client.resolve("https://youtu.be/another-one")
        assert result is None
        assert respx.calls.call_count == calls_before


@respx.mock
async def test_resolve_short_link_follows_redirect_and_caches(db: Database) -> None:
    respx.get("https://spotify.link/abc").mock(
        return_value=httpx.Response(
            302, headers={"Location": "https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6"}
        )
    )
    respx.get("https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6").mock(return_value=httpx.Response(200))

    async with httpx.AsyncClient() as http:
        result = await resolve_short_link(http, db, "https://spotify.link/abc")
        assert result == "https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6"

        # cached: a second call must not hit the network again
        calls_before = respx.calls.call_count
        result2 = await resolve_short_link(http, db, "https://spotify.link/abc")
        assert result2 == result
        assert respx.calls.call_count == calls_before


@respx.mock
async def test_resolve_short_link_no_useful_redirect_is_cached_as_miss(db: Database) -> None:
    respx.get("https://spotify.link/dead").mock(return_value=httpx.Response(200))

    async with httpx.AsyncClient() as http:
        result = await resolve_short_link(http, db, "https://spotify.link/dead")
    assert result is None
    cached = await db.get_odesli_cache("https://spotify.link/dead")
    assert cached is not None
    assert cached["miss"] == 1


def test_clean_title_strips_official_video_noise() -> None:
    assert clean_title("Artist - Song Name (Official Video)") == "Artist - Song Name"


def test_clean_title_strips_brackets_and_feat() -> None:
    cleaned = clean_title("Song Name [HD] feat. Someone Else")
    assert "[HD]" not in cleaned
    assert "feat" not in cleaned.lower()


def test_clean_title_strips_topic_suffix() -> None:
    assert clean_title("Some Artist - Topic") == "Some Artist"


@respx.mock
async def test_youtube_oembed_returns_cleaned_title_and_artist() -> None:
    respx.get("https://www.youtube.com/oembed").mock(
        return_value=httpx.Response(
            200, json={"title": "Cool Song (Official Music Video)", "author_name": "Cool Artist - Topic"}
        )
    )
    async with httpx.AsyncClient() as http:
        client = YouTubeOEmbedClient(http)
        result = await client.get_title_artist("https://www.youtube.com/watch?v=abc")
    assert result is not None
    title, artist = result
    assert title == "Cool Song"
    assert artist == "Cool Artist"


@respx.mock
async def test_youtube_oembed_404_returns_none() -> None:
    respx.get("https://www.youtube.com/oembed").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as http:
        client = YouTubeOEmbedClient(http)
        result = await client.get_title_artist("https://www.youtube.com/watch?v=deleted")
    assert result is None
