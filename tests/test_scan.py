from __future__ import annotations

from types import SimpleNamespace

import pytest

import cratebot.scan as scan_module
from cratebot.pipeline import AddOutcome, AddStatus
from cratebot.scan import run_history_scan, run_search_scan


TRACK_URL_A = "https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6"
TRACK_URL_B = "https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG7"


class FakeHTTP:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[list[tuple[str, str]]] = []

    async def request(self, route, params=None):
        self.calls.append(params or [])
        return self._responses.pop(0)


class FakeBot:
    def __init__(self, http: FakeHTTP) -> None:
        self.http = http


class FakePipeline:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, bool]] = []

    async def process_link(self, parsed, *, message_id, requester_id, dry_run=False):
        self.calls.append((parsed.normalized_url, message_id, requester_id, dry_run))
        return AddOutcome(AddStatus.DRY_RUN, "would add")


def _param_value(params: list[tuple[str, str]], key: str) -> str | None:
    for k, v in params:
        if k == key:
            return v
    return None


async def test_run_search_scan_retries_on_still_indexing_202() -> None:
    bodies = [
        {"code": 110000, "retry_after": 0, "documents_indexed": 10},
        {
            "messages": [
                [{"id": "100", "content": TRACK_URL_A, "author": {"id": "u1"}}],
                [{"id": "101", "content": "just chatting, no link", "author": {"id": "u1"}}],
            ],
            "total_results": 2,
        },
    ]
    http = FakeHTTP(bodies)
    bot = FakeBot(http)
    pipeline = FakePipeline()

    progress = await run_search_scan(bot, pipeline, guild_id=1, channel_ids=[10], dry_run=True)

    assert progress.total_seen == 2
    assert progress.added == 1
    assert len(http.calls) == 2
    assert len(pipeline.calls) == 1
    assert pipeline.calls[0][0] == TRACK_URL_A


async def test_run_search_scan_flattens_nested_messages_array() -> None:
    bodies = [
        {
            "messages": [
                [{"id": "1", "content": TRACK_URL_A, "author": {"id": "u1"}}],
                [{"id": "2", "content": TRACK_URL_B, "author": {"id": "u2"}}],
            ],
            "total_results": 2,
        },
    ]
    http = FakeHTTP(bodies)
    bot = FakeBot(http)
    pipeline = FakePipeline()

    progress = await run_search_scan(bot, pipeline, guild_id=1, channel_ids=[10], dry_run=True)

    assert progress.total_seen == 2
    assert progress.added == 2


async def test_run_search_scan_windows_forward_past_offset_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan_module, "SEARCH_OFFSET_CAP", 1)

    bodies = [
        {
            "messages": [
                [{"id": "100", "content": TRACK_URL_A, "author": {"id": "u1"}}],
                [{"id": "101", "content": TRACK_URL_B, "author": {"id": "u1"}}],
            ],
            "total_results": 5,  # more than this page returned, forces the offset-cap branch
        },
        {
            "messages": [[{"id": "200", "content": "no link here", "author": {"id": "u1"}}]],
            "total_results": 1,
        },
    ]
    http = FakeHTTP(bodies)
    bot = FakeBot(http)
    pipeline = FakePipeline()

    progress = await run_search_scan(bot, pipeline, guild_id=1, channel_ids=[10], dry_run=True)

    assert len(http.calls) == 2
    # second call must have windowed forward using the last seen message ID (101 + 1)
    assert _param_value(http.calls[1], "min_id") == "102"
    assert progress.total_seen == 3


async def test_run_search_scan_does_not_paginate_off_short_page_length() -> None:
    # a page returning fewer than `limit` items must still respect total_results, not stop early
    bodies = [
        {
            "messages": [[{"id": "1", "content": TRACK_URL_A, "author": {"id": "u1"}}]],
            "total_results": 2,
        },
        {
            "messages": [[{"id": "2", "content": TRACK_URL_B, "author": {"id": "u1"}}]],
            "total_results": 2,
        },
    ]
    http = FakeHTTP(bodies)
    bot = FakeBot(http)
    pipeline = FakePipeline()

    progress = await run_search_scan(bot, pipeline, guild_id=1, channel_ids=[10], dry_run=True)

    assert len(http.calls) == 2
    assert progress.total_seen == 2
    assert progress.added == 2


class FakeMessage:
    def __init__(self, message_id: int, content: str, author_id: str = "u1") -> None:
        self.id = message_id
        self.content = content
        self.embeds: list = []
        self.author = SimpleNamespace(id=author_id)


class FakeChannel:
    def __init__(self, channel_id: int, messages: list[FakeMessage]) -> None:
        self.id = channel_id
        self._messages = messages

    def history(self, *, limit=None, after=None, before=None, oldest_first=True):
        async def _gen():
            for message in self._messages:
                if after is not None and message.id <= after.id:
                    continue
                if before is not None and message.id >= before.id:
                    continue
                yield message

        return _gen()


async def test_run_history_scan_processes_all_messages_and_persists_cursor() -> None:
    messages = [
        FakeMessage(100, "no link"),
        FakeMessage(101, TRACK_URL_A),
        FakeMessage(102, TRACK_URL_B),
    ]
    channel = FakeChannel(10, messages)
    pipeline = FakePipeline()
    cursors: dict[str, str] = {}

    async def get_cursor(channel_id: str) -> str | None:
        return cursors.get(channel_id)

    async def set_cursor(channel_id: str, message_id: str) -> None:
        cursors[channel_id] = message_id

    progress = await run_history_scan(
        bot=None,
        pipeline=pipeline,
        channel=channel,
        resume=True,
        dry_run=True,
        get_cursor=get_cursor,
        set_cursor=set_cursor,
    )

    assert progress.total_seen == 3
    assert progress.added == 2
    assert cursors["10"] == "102"


async def test_run_history_scan_resumes_from_persisted_cursor() -> None:
    messages = [
        FakeMessage(100, "no link"),
        FakeMessage(101, TRACK_URL_A),
        FakeMessage(102, TRACK_URL_B),
    ]
    channel = FakeChannel(10, messages)
    pipeline = FakePipeline()
    cursors: dict[str, str] = {"10": "101"}

    async def get_cursor(channel_id: str) -> str | None:
        return cursors.get(channel_id)

    async def set_cursor(channel_id: str, message_id: str) -> None:
        cursors[channel_id] = message_id

    progress = await run_history_scan(
        bot=None,
        pipeline=pipeline,
        channel=channel,
        resume=True,
        dry_run=True,
        get_cursor=get_cursor,
        set_cursor=set_cursor,
    )

    # only message 102 is after the stored cursor of 101
    assert progress.total_seen == 1
    assert progress.added == 1
    assert cursors["10"] == "102"
