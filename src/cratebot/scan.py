"""Retrospective backfill: two strategies per brief 5.4.

Strategy A (search): server-side `GET /guilds/{id}/messages/search`. Far
cheaper than walking history, but gated by the same MESSAGE_CONTENT intent,
can return 202 while the guild is still being indexed, nests `messages` as
an array of arrays for legacy reasons, and caps `offset` at 9975 - beyond
that we window forward by snowflake using `max_id`/`min_id`.

Strategy B (history): exhaustive pagination of `GET /channels/{id}/messages`
via discord.py's `channel.history()`. Used as the fallback when the search
index is unavailable, and persists a resumable cursor per channel.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import discord
from discord.http import Route

from cratebot.discordbot.text_extract import collect_texts, collect_texts_from_raw
from cratebot.links.parser import parse_all
from cratebot.logging_setup import get_logger
from cratebot.pipeline import AddPipeline, AddStatus

logger = get_logger(__name__)

SEARCH_OFFSET_CAP = 9975
SEARCH_PAGE_SIZE = 25
STILL_INDEXING_CODE = 110000

ProgressCallback = Callable[["ScanProgress"], Awaitable[None]]


@dataclass
class ScanProgress:
    total_seen: int = 0
    added: int = 0
    duplicates: int = 0
    ambiguous: int = 0
    failed: int = 0
    skipped: int = 0
    still_indexing: bool = False
    strategy: str = "search"
    notes: list[str] = field(default_factory=list)

    def tally(self, status: AddStatus) -> None:
        self.total_seen += 1
        if status in (AddStatus.ADDED, AddStatus.DRY_RUN):
            self.added += 1
        elif status is AddStatus.DUPLICATE:
            self.duplicates += 1
        elif status is AddStatus.AMBIGUOUS:
            self.ambiguous += 1
        elif status is AddStatus.FAILED:
            self.failed += 1
        else:
            self.skipped += 1


class SearchUnavailableError(Exception):
    """Raised after repeated non-indexing failures, telling the caller to fall back to Strategy B."""


async def _raw_search(
    bot: discord.Client,
    guild_id: int,
    channel_ids: list[int],
    *,
    min_id: int | None,
    max_id: int | None,
    offset: int,
    limit: int = SEARCH_PAGE_SIZE,
) -> dict:
    route = Route("GET", "/guilds/{guild_id}/messages/search", guild_id=guild_id)
    params: list[tuple[str, str]] = [("channel_id", str(cid)) for cid in channel_ids]
    params += [
        ("has", "link"),
        ("author_type", "-bot"),
        ("sort_by", "timestamp"),
        ("sort_order", "asc"),
        ("limit", str(limit)),
        ("offset", str(offset)),
    ]
    if min_id is not None:
        params.append(("min_id", str(min_id)))
    if max_id is not None:
        params.append(("max_id", str(max_id)))
    return await bot.http.request(route, params=params)  # type: ignore[attr-defined]


async def _process_hit(
    pipeline: AddPipeline, msg: dict, playlist_id: str, dry_run: bool, progress: ScanProgress
) -> None:
    texts = collect_texts_from_raw(msg)

    parsed_links = []
    for text in texts:
        parsed_links.extend(parse_all(text))

    if not parsed_links:
        progress.total_seen += 1
        return

    message_id = str(msg["id"])
    author = msg.get("author") or {}
    requester_id = str(author["id"]) if author.get("id") else None

    counted = False
    for parsed in parsed_links:
        outcome = await pipeline.process_link(
            parsed, playlist_id=playlist_id, message_id=message_id, requester_id=requester_id, dry_run=dry_run
        )
        progress.tally(outcome.status)
        counted = True
    if not counted:
        progress.total_seen += 1


async def run_search_scan(
    bot: discord.Client,
    pipeline: AddPipeline,
    guild_id: int,
    channel_ids: list[int],
    *,
    playlist_id: str,
    min_id: int | None = None,
    max_id: int | None = None,
    dry_run: bool = True,
    progress_cb: ProgressCallback | None = None,
    max_consecutive_index_failures: int = 3,
) -> ScanProgress:
    progress = ScanProgress(strategy="search")
    window_min = min_id
    index_failures = 0

    while True:
        offset = 0
        window_last_id: int | None = None
        while True:
            try:
                body = await _raw_search(
                    bot, guild_id, channel_ids, min_id=window_min, max_id=max_id, offset=offset
                )
            except discord.Forbidden as exc:
                raise SearchUnavailableError(f"Search forbidden: {exc}") from exc
            except discord.HTTPException as exc:
                index_failures += 1
                if index_failures >= max_consecutive_index_failures:
                    raise SearchUnavailableError(f"Search failing repeatedly: {exc}") from exc
                continue

            if body.get("code") == STILL_INDEXING_CODE or "retry_after" in body:
                retry_after = float(body.get("retry_after") or 1)
                progress.still_indexing = True
                progress.notes.append(f"Guild still indexing, retrying in {retry_after}s")
                if progress_cb:
                    await progress_cb(progress)
                await asyncio.sleep(max(retry_after, 0.5))
                continue

            index_failures = 0
            progress.still_indexing = False
            nested = body.get("messages", [])
            flat = [m for group in nested for m in group]
            if not flat:
                break

            # One commit per page (<=25 messages) instead of one per processed link -
            # a full backfill would otherwise be an fsync-round-trip per row.
            async with pipeline.db.batch():
                for msg in flat:
                    await _process_hit(pipeline, msg, playlist_id, dry_run, progress)
                    window_last_id = int(msg["id"])
            if progress_cb and progress.total_seen % 10 == 0:
                await progress_cb(progress)

            total_results = body.get("total_results", 0)
            offset += len(flat)
            if offset >= total_results:
                break
            if offset > SEARCH_OFFSET_CAP:
                break  # window forward below

        if window_last_id is None:
            break  # nothing at all in this window; fully done
        if offset <= SEARCH_OFFSET_CAP:
            break  # exhausted the range without hitting the cap
        window_min = window_last_id + 1  # walk the next snowflake window

    if progress_cb:
        await progress_cb(progress)
    return progress


async def run_history_scan(
    bot: discord.Client,
    pipeline: AddPipeline,
    channel: discord.abc.Messageable,
    *,
    playlist_id: str,
    resume: bool = True,
    after_id: int | None = None,
    before_id: int | None = None,
    dry_run: bool = True,
    progress_cb: ProgressCallback | None = None,
    get_cursor: Callable[[str], Awaitable[str | None]] | None = None,
    set_cursor: Callable[[str, str], Awaitable[None]] | None = None,
) -> ScanProgress:
    progress = ScanProgress(strategy="history")
    channel_id = str(getattr(channel, "id", "unknown"))

    after: discord.Object | None = discord.Object(id=after_id) if after_id else None
    if resume and after is None and get_cursor is not None:
        cursor = await get_cursor(channel_id)
        if cursor:
            after = discord.Object(id=int(cursor))
    before: discord.Object | None = discord.Object(id=before_id) if before_id else None

    # Commit every CHECKPOINT_EVERY messages instead of after each individual write -
    # a full history walk would otherwise be an fsync-round-trip per row. Worst case
    # on a crash mid-batch is redoing up to CHECKPOINT_EVERY messages' work, which is
    # harmless (the dedup checks that would let them re-add something are rolled back
    # right along with the uncommitted writes).
    CHECKPOINT_EVERY = 10
    async with pipeline.db.batch():
        async for message in channel.history(limit=None, after=after, before=before, oldest_first=True):
            texts = collect_texts(message)
            parsed_links = [p for text in texts for p in parse_all(text)]
            if not parsed_links:
                progress.total_seen += 1
            for parsed in parsed_links:
                outcome = await pipeline.process_link(
                    parsed,
                    playlist_id=playlist_id,
                    message_id=str(message.id),
                    requester_id=str(message.author.id),
                    dry_run=dry_run,
                )
                progress.tally(outcome.status)

            if set_cursor is not None:
                await set_cursor(channel_id, str(message.id))

            if progress.total_seen % CHECKPOINT_EVERY == 0:
                await pipeline.db.checkpoint()
                if progress_cb:
                    await progress_cb(progress)

    if progress_cb:
        await progress_cb(progress)
    return progress
