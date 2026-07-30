"""Pure, I/O-free link detection and classification.

Handles the URL forms in the build brief section 5.1: bare track/album/
playlist/episode links, the `intl-xx` locale prefix, the `spotify:` URI
scheme, `spotify.link` short links (flagged, not resolved here), and
query-param noise (`si`, `utm_source`, `nd`, etc). Also detects
best-effort links from other music platforms so the resolver can attempt
cross-platform matching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit, urlunsplit

# A bare URL or a spotify: URI, as they'd appear inline in chat text.
_URL_TOKEN_RE = re.compile(
    r"(https?://[^\s<>\[\]()]+|spotify:(?:track|album|playlist|episode|show):[A-Za-z0-9]+)",
    re.IGNORECASE,
)

# Trailing punctuation that commonly gets swept up when a link ends a sentence.
_TRAILING_PUNCT_RE = re.compile(r"[.,;:!?)\]>'\"]+$")

_SPOTIFY_HOST_RE = re.compile(
    r"^open\.spotify\.com$",
    re.IGNORECASE,
)
_SPOTIFY_PATH_RE = re.compile(
    r"^/(?:intl-[a-zA-Z-]+/)?(track|album|playlist|episode|show)/([A-Za-z0-9]+)",
)
_SPOTIFY_URI_RE = re.compile(
    r"^spotify:(track|album|playlist|episode|show):([A-Za-z0-9]+)$",
    re.IGNORECASE,
)
_SPOTIFY_SHORT_HOST_RE = re.compile(r"^spotify\.link$", re.IGNORECASE)


class Platform(str, Enum):
    SPOTIFY = "spotify"
    YOUTUBE = "youtube"
    APPLE_MUSIC = "apple_music"
    SOUNDCLOUD = "soundcloud"
    BANDCAMP = "bandcamp"
    TIDAL = "tidal"
    DEEZER = "deezer"
    AMAZON_MUSIC = "amazon_music"
    UNKNOWN = "unknown"


_OTHER_PLATFORM_HOST_PATTERNS: list[tuple[Platform, re.Pattern[str]]] = [
    (Platform.YOUTUBE, re.compile(r"^(www\.|music\.)?youtube\.com$|^youtu\.be$", re.IGNORECASE)),
    (Platform.APPLE_MUSIC, re.compile(r"^music\.apple\.com$", re.IGNORECASE)),
    (Platform.SOUNDCLOUD, re.compile(r"^(www\.)?soundcloud\.com$", re.IGNORECASE)),
    (Platform.BANDCAMP, re.compile(r"^[\w-]+\.bandcamp\.com$", re.IGNORECASE)),
    (Platform.TIDAL, re.compile(r"^(listen\.)?tidal\.com$", re.IGNORECASE)),
    (Platform.DEEZER, re.compile(r"^(www\.)?deezer\.com$", re.IGNORECASE)),
    (Platform.AMAZON_MUSIC, re.compile(r"^music\.amazon\.\w+$", re.IGNORECASE)),
]


@dataclass(frozen=True)
class ParsedLink:
    raw_url: str
    normalized_url: str
    platform: Platform
    spotify_type: str | None = None  # track | album | playlist | episode | show
    spotify_id: str | None = None
    is_short_link: bool = False

    @property
    def is_spotify(self) -> bool:
        return self.platform is Platform.SPOTIFY


def extract_urls(text: str) -> list[str]:
    """Find candidate URL/URI tokens in free text (message content, embed URLs, etc.)."""
    if not text:
        return []
    found: list[str] = []
    for match in _URL_TOKEN_RE.finditer(text):
        token = _TRAILING_PUNCT_RE.sub("", match.group(0))
        if token:
            found.append(token)
    return found


def _strip_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def parse_link(url: str) -> ParsedLink | None:
    """Classify a single URL/URI. Returns None if it isn't recognisable as anything useful."""
    url = url.strip()
    if not url:
        return None

    uri_match = _SPOTIFY_URI_RE.match(url)
    if uri_match:
        spotify_type, spotify_id = uri_match.group(1).lower(), uri_match.group(2)
        normalized = f"https://open.spotify.com/{spotify_type}/{spotify_id}"
        return ParsedLink(
            raw_url=url,
            normalized_url=normalized,
            platform=Platform.SPOTIFY,
            spotify_type=spotify_type,
            spotify_id=spotify_id,
        )

    parts = urlsplit(url)
    if not parts.scheme.startswith("http"):
        return None
    host = parts.netloc.lower()

    if _SPOTIFY_HOST_RE.match(host):
        path_match = _SPOTIFY_PATH_RE.match(parts.path)
        if not path_match:
            return None
        spotify_type, spotify_id = path_match.group(1).lower(), path_match.group(2)
        normalized = f"https://open.spotify.com/{spotify_type}/{spotify_id}"
        return ParsedLink(
            raw_url=url,
            normalized_url=normalized,
            platform=Platform.SPOTIFY,
            spotify_type=spotify_type,
            spotify_id=spotify_id,
        )

    if _SPOTIFY_SHORT_HOST_RE.match(host):
        return ParsedLink(
            raw_url=url,
            normalized_url=_strip_query(url),
            platform=Platform.SPOTIFY,
            is_short_link=True,
        )

    for platform, pattern in _OTHER_PLATFORM_HOST_PATTERNS:
        if pattern.match(host):
            return ParsedLink(
                raw_url=url,
                normalized_url=_strip_query(url),
                platform=platform,
            )

    return None


def parse_all(text: str) -> list[ParsedLink]:
    """Extract and classify every recognisable music link in a block of text."""
    parsed: list[ParsedLink] = []
    for token in extract_urls(text):
        link = parse_link(token)
        if link is not None:
            parsed.append(link)
    return parsed
