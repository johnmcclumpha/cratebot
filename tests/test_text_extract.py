from __future__ import annotations

from types import SimpleNamespace

from cratebot.discordbot.text_extract import collect_texts, collect_texts_from_raw


class FakeEmbed:
    def __init__(self, url: str | None) -> None:
        self.url = url


class FakeMessage:
    def __init__(self, content: str, embeds: list[FakeEmbed], snapshots: list) -> None:
        self.content = content
        self.embeds = embeds
        self.message_snapshots = snapshots


def test_collect_texts_includes_content_embed_urls_and_snapshots() -> None:
    message = FakeMessage(
        content="check this out https://open.spotify.com/track/abc",
        embeds=[FakeEmbed("https://open.spotify.com/track/abc"), FakeEmbed(None)],
        snapshots=[SimpleNamespace(message=SimpleNamespace(content="forwarded: https://open.spotify.com/track/xyz"))],
    )
    texts = collect_texts(message)
    assert texts == [
        "check this out https://open.spotify.com/track/abc",
        "https://open.spotify.com/track/abc",
        "forwarded: https://open.spotify.com/track/xyz",
    ]


def test_collect_texts_from_raw_matches_the_same_shape() -> None:
    """Strategy A's search-hit dicts should extract identically to the equivalent
    discord.Message, since both feed process_link the same way."""
    raw = {
        "content": "check this out https://open.spotify.com/track/abc",
        "embeds": [{"url": "https://open.spotify.com/track/abc"}, {}],
        "message_snapshots": [{"message": {"content": "forwarded: https://open.spotify.com/track/xyz"}}],
    }
    assert collect_texts_from_raw(raw) == [
        "check this out https://open.spotify.com/track/abc",
        "https://open.spotify.com/track/abc",
        "forwarded: https://open.spotify.com/track/xyz",
    ]


def test_collect_texts_from_raw_handles_missing_fields() -> None:
    assert collect_texts_from_raw({}) == [""]
