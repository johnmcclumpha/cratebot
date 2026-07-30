"""Shared helper: pull every scannable text blob out of a Discord message.

Used by both live monitoring and the history-walk backfill strategy so the
two paths can't silently drift apart on what counts as "the message text"
(content, embed URLs, forwarded-message snapshots).
"""

from __future__ import annotations

import discord


def collect_texts(message: discord.Message) -> list[str]:
    texts: list[str] = [message.content or ""]
    for embed in message.embeds:
        if embed.url:
            texts.append(embed.url)
    for snapshot in getattr(message, "message_snapshots", None) or []:
        snap_message = getattr(snapshot, "message", snapshot)
        content = getattr(snap_message, "content", None)
        if content:
            texts.append(content)
    return texts
