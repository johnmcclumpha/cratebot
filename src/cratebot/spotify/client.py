"""Thin async wrapper around the Spotify Web API (2026 endpoint shapes).

Verified against the brief's build-time notes: playlist sub-resources live
under /items (not /tracks), batch track lookup is gone (one at a time,
bounded concurrency), search limit tops out at 10, and several track/user
fields were removed. See section 2.2 of the build brief and re-verify
against https://developer.spotify.com/documentation/web-api/references/changes/
if Spotify has moved things again.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx

from cratebot.ratelimit import backoff_sleep
from cratebot.spotify.errors import SpotifyAPIError, SpotifyQuotaExceededError

API_BASE = "https://api.spotify.com/v1"
MAX_SEARCH_LIMIT = 10  # down from 50 as of the 2026 changes
MAX_ADD_BATCH = 100
MAX_RETRIES = 4

TokenProvider = Callable[[], Awaitable[str]]


def extract_track_entry(playlist_item_entry: dict) -> dict | None:
    """Playlist item entries renamed `track` -> `item`; support both defensively."""
    return playlist_item_entry.get("item") or playlist_item_entry.get("track")


def playlist_total(playlist: dict) -> int:
    """Playlist object field renamed `tracks` -> `items`; code defensively either way."""
    items_obj = playlist.get("items") or playlist.get("tracks") or {}
    return items_obj.get("total", 0) if isinstance(items_obj, dict) else 0


class SpotifyClient:
    def __init__(self, http: httpx.AsyncClient, token_provider: TokenProvider) -> None:
        self._http = http
        self._token_provider = token_provider

    async def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        url = f"{API_BASE}{path}"
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            token = await self._token_provider()
            headers = dict(kwargs.pop("headers", None) or {})
            headers["Authorization"] = f"Bearer {token}"
            response = await self._http.request(method, url, headers=headers, **kwargs)

            if response.status_code == 429:
                body = _safe_json(response)
                reason = body.get("error", {}).get("reason")
                if reason == "QUOTA_EXCEEDED":
                    raise SpotifyQuotaExceededError(
                        "Spotify Dev Mode quota exceeded; backing off hard, retrying will not help."
                    )
                retry_after = float(response.headers.get("Retry-After", "1"))
                await asyncio.sleep(retry_after)
                last_error = SpotifyAPIError(429, "rate limited", body)
                continue

            if response.status_code >= 500:
                await backoff_sleep(attempt)
                last_error = SpotifyAPIError(response.status_code, "server error", _safe_json(response))
                continue

            if response.status_code >= 400:
                body = _safe_json(response)
                message = body.get("error", {}).get("message", response.text)
                raise SpotifyAPIError(response.status_code, message, body)

            return response

        assert last_error is not None
        raise last_error

    # -- identity / ownership --------------------------------------------

    async def get_me(self) -> dict:
        response = await self._request("GET", "/me")
        return response.json()

    async def get_playlist(self, playlist_id: str) -> dict:
        response = await self._request("GET", f"/playlists/{playlist_id}")
        return response.json()

    # -- tracks ------------------------------------------------------------

    async def get_track(self, track_id: str) -> dict:
        response = await self._request("GET", f"/tracks/{track_id}")
        return response.json()

    async def search_tracks(self, query: str, limit: int = MAX_SEARCH_LIMIT, offset: int = 0) -> dict:
        limit = min(limit, MAX_SEARCH_LIMIT)
        response = await self._request(
            "GET",
            "/search",
            params={"q": query, "type": "track", "limit": limit, "offset": offset},
        )
        return response.json()

    async def search_tracks_paginated(
        self, query: str, max_candidates: int = 30
    ) -> list[dict]:
        """Paginate with offset since the per-request limit is capped at 10."""
        candidates: list[dict] = []
        offset = 0
        while len(candidates) < max_candidates:
            page = await self.search_tracks(query, limit=MAX_SEARCH_LIMIT, offset=offset)
            items = page.get("tracks", {}).get("items", [])
            if not items:
                break
            candidates.extend(items)
            offset += len(items)
            if offset >= page.get("tracks", {}).get("total", 0):
                break
        return candidates[:max_candidates]

    # -- albums --------------------------------------------------------------

    async def get_album_tracks(self, album_id: str, limit: int = 50) -> list[dict]:
        tracks: list[dict] = []
        offset = 0
        while True:
            response = await self._request(
                "GET", f"/albums/{album_id}/tracks", params={"limit": limit, "offset": offset}
            )
            page = response.json()
            items = page.get("items", [])
            tracks.extend(items)
            if not page.get("next"):
                break
            offset += len(items)
        return tracks

    # -- playlist items ---------------------------------------------------

    async def iter_playlist_items(self, playlist_id: str, page_size: int = 100) -> AsyncIterator[dict]:
        offset = 0
        while True:
            response = await self._request(
                "GET",
                f"/playlists/{playlist_id}/items",
                params={"limit": page_size, "offset": offset},
            )
            page = response.json()
            items = page.get("items", [])
            for entry in items:
                yield entry
            if not items or not page.get("next"):
                break
            offset += len(items)

    async def add_items(self, playlist_id: str, uris: list[str]) -> None:
        for i in range(0, len(uris), MAX_ADD_BATCH):
            batch = uris[i : i + MAX_ADD_BATCH]
            await self._request(
                "POST",
                f"/playlists/{playlist_id}/items",
                json={"uris": batch},
            )


def _safe_json(response: httpx.Response) -> dict:
    try:
        return response.json()
    except ValueError:
        return {}
