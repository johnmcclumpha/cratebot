from __future__ import annotations

import pytest

from cratebot.links.parser import Platform, dedupe_by_normalized_url, parse_all, parse_link

TRACK_ID = "6rqhFgbbKwnb9MLmUQDhG6"

TRACK_CASES = [
    ("https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6", TRACK_ID),
    ("https://open.spotify.com/intl-de/track/6rqhFgbbKwnb9MLmUQDhG6", TRACK_ID),
    ("https://open.spotify.com/intl-pt-br/track/6rqhFgbbKwnb9MLmUQDhG6", TRACK_ID),
    ("spotify:track:6rqhFgbbKwnb9MLmUQDhG6", TRACK_ID),
    (
        "https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6?si=abc123&utm_source=copy-link",
        TRACK_ID,
    ),
    ("https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6?nd=1", TRACK_ID),
]


@pytest.mark.parametrize("url,expected_id", TRACK_CASES)
def test_track_links_parse_to_same_id(url: str, expected_id: str) -> None:
    parsed = parse_link(url)
    assert parsed is not None
    assert parsed.platform is Platform.SPOTIFY
    assert parsed.spotify_type == "track"
    assert parsed.spotify_id == expected_id
    assert parsed.normalized_url == f"https://open.spotify.com/track/{expected_id}"


def test_album_link() -> None:
    parsed = parse_link("https://open.spotify.com/album/1a2b3c4d5e6f7g8h9i0j1k")
    assert parsed is not None
    assert parsed.spotify_type == "album"


def test_playlist_link() -> None:
    parsed = parse_link("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
    assert parsed is not None
    assert parsed.spotify_type == "playlist"


def test_episode_link() -> None:
    parsed = parse_link("https://open.spotify.com/episode/512ojhOuo1ktJprKbVcKyQ")
    assert parsed is not None
    assert parsed.spotify_type == "episode"


def test_short_link_flagged_not_resolved() -> None:
    parsed = parse_link("https://spotify.link/abc123XYZ")
    assert parsed is not None
    assert parsed.is_short_link is True
    assert parsed.platform is Platform.SPOTIFY


@pytest.mark.parametrize(
    "url,expected_platform",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", Platform.YOUTUBE),
        ("https://youtu.be/dQw4w9WgXcQ", Platform.YOUTUBE),
        ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", Platform.YOUTUBE),
        ("https://music.apple.com/us/album/foo/123456", Platform.APPLE_MUSIC),
        ("https://soundcloud.com/artist/track-name", Platform.SOUNDCLOUD),
        ("https://someartist.bandcamp.com/track/song", Platform.BANDCAMP),
        ("https://listen.tidal.com/track/12345", Platform.TIDAL),
        ("https://www.deezer.com/track/12345", Platform.DEEZER),
        ("https://music.amazon.com/albums/B01ABC", Platform.AMAZON_MUSIC),
    ],
)
def test_other_platforms_detected(url: str, expected_platform: Platform) -> None:
    parsed = parse_link(url)
    assert parsed is not None
    assert parsed.platform is expected_platform


@pytest.mark.parametrize(
    "url,expected_normalized",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        # query noise (playlist/index/share tracking) must be dropped, but not `v`
        (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLxyz&index=3",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ&feature=share",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        # youtu.be and /shorts/ already carry the ID in the path, not a query param
        ("https://youtu.be/dQw4w9WgXcQ?si=abc123", "https://youtu.be/dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "https://www.youtube.com/shorts/dQw4w9WgXcQ"),
    ],
)
def test_youtube_normalized_url_keeps_the_video_id(url: str, expected_normalized: str) -> None:
    parsed = parse_link(url)
    assert parsed is not None
    assert parsed.normalized_url == expected_normalized


def test_youtube_different_videos_do_not_collide_after_normalization() -> None:
    """Regression: a blanket query-strip used to normalize every youtube.com/watch
    link down to the same bare '.../watch' URL, which broke Odesli/oEmbed
    resolution and made dedup treat two different videos as the same link."""
    first = parse_link("https://www.youtube.com/watch?v=aaaaaaaaaaa")
    second = parse_link("https://www.youtube.com/watch?v=bbbbbbbbbbb")
    assert first is not None and second is not None
    assert first.normalized_url != second.normalized_url


def test_unknown_url_returns_none() -> None:
    assert parse_link("https://example.com/not-music") is None


def test_non_url_text_returns_none() -> None:
    assert parse_link("just chatting about music") is None


def test_extract_and_parse_all_from_message_text() -> None:
    text = (
        "check this out https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6?si=xyz "
        "and also spotify:track:6rqhFgbbKwnb9MLmUQDhG6 (dup id) plus "
        "https://youtu.be/dQw4w9WgXcQ."
    )
    parsed = parse_all(text)
    assert len(parsed) == 3
    assert parsed[0].spotify_id == TRACK_ID
    assert parsed[1].spotify_id == TRACK_ID
    assert parsed[2].platform is Platform.YOUTUBE
    # trailing period must not leak into the extracted URL
    assert not parsed[2].raw_url.endswith(".")


def test_trailing_punctuation_stripped() -> None:
    parsed = parse_all("Listen: https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6).")
    assert len(parsed) == 1
    assert parsed[0].spotify_id == TRACK_ID


def test_forwarded_and_embed_style_bare_uri_in_content() -> None:
    parsed = parse_all("spotify:track:6rqhFgbbKwnb9MLmUQDhG6")
    assert len(parsed) == 1
    assert parsed[0].spotify_id == TRACK_ID


def test_dedupe_by_normalized_url_collapses_repeats_preserving_order() -> None:
    """Regression: the same link commonly appears twice in one message - once
    in the raw content, once more in Discord's own link-preview embed once it
    attaches one. Every caller that scans both must dedupe before handing
    links to the pipeline, or the same link gets processed twice per message
    (previously reproduced via the context-menu "Add to playlist" command,
    which - unlike live monitoring - didn't dedupe: it showed a
    disambiguation prompt, then immediately followed up with "already
    processed this link" for the same right-click)."""
    a = parse_link("https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6")
    b = parse_link("https://youtu.be/dQw4w9WgXcQ")
    a_again = parse_link("https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6?si=xyz")  # same normalized_url as a
    assert a is not None and b is not None and a_again is not None

    deduped = dedupe_by_normalized_url([a, b, a_again])
    assert deduped == [a, b]


def test_dedupe_by_normalized_url_empty_list() -> None:
    assert dedupe_by_normalized_url([]) == []
