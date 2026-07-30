"""Cross-platform link resolution: Odesli primary, oEmbed+search fallback.

Odesli's free tier is the tightest budget in the whole system (~10 req/min
per IP, no key required) so every resolution is cached permanently and
gated behind a token bucket. Treat it as best-effort third-party
infrastructure: every call has a timeout, and failures degrade to "couldn't
resolve" rather than raising.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from cratebot.db import Database
from cratebot.links.parser import parse_link
from cratebot.logging_setup import get_logger
from cratebot.ratelimit import CircuitBreaker, TokenBucket

logger = get_logger(__name__)

ODESLI_URL = "https://api.song.link/v1-alpha.1/links"
ODESLI_DEFAULT_RATE_PER_MINUTE = 10
YOUTUBE_OEMBED_URL = "https://www.youtube.com/oembed"

_NOISE_PATTERNS = [
    re.compile(r"\(.*?(official|video|audio|lyric|lyrics|hd|4k|visualizer).*?\)", re.IGNORECASE),
    re.compile(r"\[.*?(official|video|audio|lyric|lyrics|hd|4k|visualizer).*?\]", re.IGNORECASE),
    re.compile(r"\bofficial\s+(music\s+)?video\b", re.IGNORECASE),
    re.compile(r"\bofficial\s+audio\b", re.IGNORECASE),
    re.compile(r"\blyric(s)?\s+video\b", re.IGNORECASE),
    re.compile(r"\s*-\s*topic$", re.IGNORECASE),
    re.compile(r"\bfeat\.?\b.*$", re.IGNORECASE),
    re.compile(r"\bft\.?\b.*$", re.IGNORECASE),
]


def clean_title(raw: str) -> str:
    cleaned = raw
    for pattern in _NOISE_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" -|")


@dataclass(frozen=True)
class OdesliResult:
    spotify_url: str
    spotify_track_id: str
    title: str | None
    artist: str | None


class OdesliClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        db: Database,
        api_key: str = "",
        rate_per_minute: float = ODESLI_DEFAULT_RATE_PER_MINUTE,
    ) -> None:
        self._http = http
        self._db = db
        self._api_key = api_key
        self._bucket = TokenBucket(rate_per_minute)
        self._circuit = CircuitBreaker(failure_threshold=5, reset_after=120.0)

    async def resolve(self, source_url: str) -> OdesliResult | None:
        cached = await self._db.get_odesli_cache(source_url)
        if cached is not None:
            if cached["miss"]:
                return None
            if cached["spotify_url"]:
                parsed = parse_link(cached["spotify_url"])
                if parsed and parsed.spotify_id:
                    return OdesliResult(
                        spotify_url=cached["spotify_url"],
                        spotify_track_id=parsed.spotify_id,
                        title=cached["title"],
                        artist=cached["artist"],
                    )
            return None

        if self._circuit.is_open:
            logger.warning("odesli.circuit_open", url=source_url)
            return None

        await self._bucket.acquire()
        params: dict[str, str] = {"url": source_url}
        if self._api_key:
            params["key"] = self._api_key

        try:
            response = await self._http.get(ODESLI_URL, params=params, timeout=10.0)
        except httpx.TimeoutException:
            self._circuit.record_failure()
            logger.warning("odesli.timeout", url=source_url)
            return None
        except httpx.HTTPError as exc:
            self._circuit.record_failure()
            logger.warning("odesli.error", url=source_url, error=str(exc))
            return None

        if response.status_code == 404:
            self._circuit.record_success()
            await self._db.set_odesli_cache(source_url, None, None, None, miss=True)
            return None

        if response.status_code >= 500:
            self._circuit.record_failure()
            return None

        if response.status_code != 200:
            self._circuit.record_failure()
            logger.warning("odesli.unexpected_status", url=source_url, status=response.status_code)
            return None

        self._circuit.record_success()
        payload = response.json()
        spotify_link = payload.get("linksByPlatform", {}).get("spotify")
        if not spotify_link or not spotify_link.get("url"):
            await self._db.set_odesli_cache(source_url, None, None, None, miss=True)
            return None

        spotify_url = spotify_link["url"]
        parsed = parse_link(spotify_url)
        if parsed is None or parsed.spotify_id is None:
            await self._db.set_odesli_cache(source_url, None, None, None, miss=True)
            return None

        entity_id = spotify_link.get("entityUniqueId")
        entity = payload.get("entitiesByUniqueId", {}).get(entity_id, {})
        title = entity.get("title")
        artist = entity.get("artistName")

        await self._db.set_odesli_cache(source_url, spotify_url, title, artist, miss=False)
        return OdesliResult(
            spotify_url=spotify_url,
            spotify_track_id=parsed.spotify_id,
            title=title,
            artist=artist,
        )


async def resolve_short_link(http: httpx.AsyncClient, db: Database, short_url: str) -> str | None:
    """Follow a spotify.link short link to its canonical open.spotify.com URL, caching the result."""
    cached = await db.get_odesli_cache(short_url)
    if cached is not None:
        return None if cached["miss"] else cached["spotify_url"]

    try:
        response = await http.get(short_url, follow_redirects=True, timeout=5.0)
    except httpx.HTTPError as exc:
        logger.warning("spotify_link.resolve_failed", url=short_url, error=str(exc))
        return None

    final_url = str(response.url)
    parsed = parse_link(final_url)
    if parsed is None or parsed.is_short_link:
        await db.set_odesli_cache(short_url, None, None, None, miss=True)
        return None

    await db.set_odesli_cache(short_url, parsed.normalized_url, None, None, miss=False)
    return parsed.normalized_url


class YouTubeOEmbedClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def get_title_artist(self, url: str) -> tuple[str, str] | None:
        try:
            response = await self._http.get(
                YOUTUBE_OEMBED_URL, params={"url": url, "format": "json"}, timeout=8.0
            )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        payload = response.json()
        raw_title = payload.get("title", "")
        uploader = payload.get("author_name", "")
        if not raw_title:
            return None
        return clean_title(raw_title), clean_title(uploader)
