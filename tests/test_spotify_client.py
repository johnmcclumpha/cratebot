from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
import respx

from cratebot.spotify.client import SpotifyClient, extract_track_entry, playlist_total
from cratebot.spotify.errors import SpotifyAPIError, SpotifyQuotaExceededError



async def _fake_token() -> str:
    return "test-token"


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient() as http:
        yield SpotifyClient(http, _fake_token)


@respx.mock
async def test_get_track_success(client: SpotifyClient) -> None:
    respx.get("https://api.spotify.com/v1/tracks/abc123").mock(
        return_value=httpx.Response(200, json={"id": "abc123", "name": "Song", "uri": "spotify:track:abc123"})
    )
    track = await client.get_track("abc123")
    assert track["name"] == "Song"


@respx.mock
async def test_get_track_404_raises_api_error(client: SpotifyClient) -> None:
    respx.get("https://api.spotify.com/v1/tracks/missing").mock(
        return_value=httpx.Response(404, json={"error": {"status": 404, "message": "not found"}})
    )
    with pytest.raises(SpotifyAPIError) as exc_info:
        await client.get_track("missing")
    assert exc_info.value.status_code == 404


@respx.mock
async def test_403_ownership_error_propagates(client: SpotifyClient) -> None:
    respx.post("https://api.spotify.com/v1/playlists/pl1/items").mock(
        return_value=httpx.Response(
            403, json={"error": {"status": 403, "message": "You cannot add tracks to a playlist you don't own"}}
        )
    )
    with pytest.raises(SpotifyAPIError) as exc_info:
        await client.add_items("pl1", ["spotify:track:abc"])
    assert exc_info.value.status_code == 403
    assert "don't own" in exc_info.value.message


@respx.mock
async def test_429_quota_exceeded_raises_immediately(client: SpotifyClient) -> None:
    route = respx.get("https://api.spotify.com/v1/tracks/x").mock(
        return_value=httpx.Response(
            429, json={"error": {"status": 429, "message": "quota", "reason": "QUOTA_EXCEEDED"}}
        )
    )
    with pytest.raises(SpotifyQuotaExceededError):
        await client.get_track("x")
    # must not retry a quota-exceeded error - retrying will not help
    assert route.call_count == 1


@respx.mock
async def test_429_burst_rate_limit_honours_retry_after_then_succeeds(client: SpotifyClient) -> None:
    route = respx.get("https://api.spotify.com/v1/tracks/y")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}, json={"error": {"status": 429, "message": "slow down"}}),
        httpx.Response(200, json={"id": "y", "name": "Retried Song"}),
    ]
    track = await client.get_track("y")
    assert track["name"] == "Retried Song"
    assert route.call_count == 2


@respx.mock
async def test_search_limit_clamped_to_10(client: SpotifyClient) -> None:
    route = respx.get("https://api.spotify.com/v1/search").mock(
        return_value=httpx.Response(200, json={"tracks": {"items": [], "total": 0}})
    )
    await client.search_tracks("some query", limit=50)
    assert route.calls.last.request.url.params["limit"] == "10"


@respx.mock
async def test_add_items_batches_at_100(client: SpotifyClient) -> None:
    route = respx.post("https://api.spotify.com/v1/playlists/pl1/items").mock(
        return_value=httpx.Response(200, json={"snapshot_id": "x"})
    )
    uris = [f"spotify:track:{i:022d}" for i in range(150)]
    await client.add_items("pl1", uris)
    assert route.call_count == 2
    first_body = route.calls[0].request.content
    assert first_body is not None


def test_playlist_total_reads_new_items_key() -> None:
    assert playlist_total({"items": {"total": 42}}) == 42


def test_playlist_total_falls_back_to_legacy_tracks_key() -> None:
    assert playlist_total({"tracks": {"total": 7}}) == 7


def test_playlist_total_defaults_to_zero() -> None:
    assert playlist_total({}) == 0


def test_extract_track_entry_prefers_item_key() -> None:
    assert extract_track_entry({"item": {"id": "1"}})["id"] == "1"


def test_extract_track_entry_falls_back_to_track_key() -> None:
    assert extract_track_entry({"track": {"id": "2"}})["id"] == "2"
