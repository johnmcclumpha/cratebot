"""Shared helper: pull every scannable text blob out of a Discord message.

Used by live monitoring, the history-walk backfill strategy (Strategy B), and
the guild-search backfill strategy (Strategy A) so the three paths can't
silently drift apart on what counts as "the message text" (content, embed
URLs, forwarded-message snapshots). Strategy A works against raw JSON dicts
from the REST search endpoint rather than discord.Message objects, so both
shapes funnel through the same `_collect_texts` core.
"""

from __future__ import annotations

import discord


def _collect_texts(content: str, embed_urls: list[str], snapshot_contents: list[str]) -> list[str]:
    return [content, *embed_urls, *snapshot_contents]


def collect_texts(message: discord.Message) -> list[str]:
    embed_urls = [embed.url for embed in message.embeds if embed.url]
    snapshot_contents = []
    for snapshot in getattr(message, "message_snapshots", None) or []:
        snap_message = getattr(snapshot, "message", snapshot)
        content = getattr(snap_message, "content", None)
        if content:
            snapshot_contents.append(content)
    return _collect_texts(message.content or "", embed_urls, snapshot_contents)


def collect_texts_from_raw(msg: dict) -> list[str]:
    """Same extraction as collect_texts(), for the raw message dicts returned by
    the guild search REST endpoint (Strategy A), which aren't discord.Message
    objects."""
    embed_urls = [url for embed in (msg.get("embeds") or []) if (url := embed.get("url"))]
    snapshot_contents = []
    for snapshot in msg.get("message_snapshots") or []:
        snap_msg = snapshot.get("message", snapshot)
        content = snap_msg.get("content")
        if content:
            snapshot_contents.append(content)
    return _collect_texts(msg.get("content") or "", embed_urls, snapshot_contents)
