"""SQLite persistence layer (aiosqlite, WAL mode).

Deliberately stores the minimum needed to operate: message/channel/author
IDs and extracted links/track IDs, never raw message text (see PRIVACY.md).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from cratebot.logging_setup import get_logger

logger = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_tokens (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    access_token_encrypted TEXT,
    refresh_token_encrypted TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_links (
    message_id TEXT NOT NULL,
    link TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (message_id, link)
);

CREATE TABLE IF NOT EXISTS added_tracks (
    playlist_id TEXT NOT NULL,
    track_id TEXT NOT NULL,
    uri TEXT NOT NULL,
    title TEXT,
    artist TEXT,
    added_at TEXT NOT NULL,
    source_message_id TEXT,
    requester_id TEXT,
    PRIMARY KEY (playlist_id, track_id)
);

CREATE TABLE IF NOT EXISTS suppressed_tracks (
    playlist_id TEXT NOT NULL,
    track_id TEXT NOT NULL,
    removed_at TEXT NOT NULL,
    reason TEXT,
    PRIMARY KEY (playlist_id, track_id)
);

CREATE TABLE IF NOT EXISTS scan_cursors (
    channel_id TEXT PRIMARY KEY,
    last_message_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS odesli_cache (
    source_url TEXT PRIMARY KEY,
    spotify_url TEXT,
    title TEXT,
    artist TEXT,
    resolved_at TEXT NOT NULL,
    miss INTEGER NOT NULL DEFAULT 0
);
"""

# Tables that gained a playlist_id column (and a composite primary key) when
# multi-guild/multi-playlist support was added. Pre-existing databases have
# these without playlist_id at all - see _migrate_legacy_dedup_tables.
_LEGACY_MIGRATABLE_TABLES = ("added_tracks", "suppressed_tracks")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._autocommit = True

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected; call connect() first")
        return self._conn

    async def connect(self, fallback_playlist_id: str | None = None) -> None:
        if self._path != ":memory:":
            parent = Path(self._path).parent
            if parent and not parent.exists():
                os.makedirs(parent, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        # Create any missing tables first (a no-op for ones that already exist,
        # including old-schema added_tracks/suppressed_tracks) so the migration
        # below can rely on e.g. runtime_config existing to check for an override.
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        await self._migrate_legacy_dedup_tables(fallback_playlist_id)

    async def _migrate_legacy_dedup_tables(self, fallback_playlist_id: str | None) -> None:
        """One-time migration: added_tracks/suppressed_tracks used to have no
        playlist_id column (the bot only ever supported one global playlist).
        If an old-schema table is found, backfill every existing row with a
        resolved playlist ID - before multi-guild support, all dedup data
        implicitly belonged to one playlist. Idempotent: a no-op once migrated,
        and a no-op on a fresh database (SCHEMA creates the new shape directly).
        """
        for table in _LEGACY_MIGRATABLE_TABLES:
            cur = await self._conn.execute(f"PRAGMA table_info({table})")
            columns = {row[1] for row in await cur.fetchall()}
            await cur.close()

            if not columns:
                continue  # table doesn't exist yet - SCHEMA below creates it fresh
            if "playlist_id" in columns:
                continue  # already migrated (or created fresh with the new schema)

            playlist_id = await self.get_config("playlist_id") or fallback_playlist_id
            if not playlist_id:
                raise RuntimeError(
                    f"'{table}' needs a one-time migration to add playlist_id, but no "
                    "playlist ID could be resolved to backfill existing rows with. "
                    "Configure at least one entry in GUILDS in .env before starting."
                )

            await self._conn.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy")
            await self._conn.executescript(SCHEMA)  # recreates `table` with the new columns
            if table == "added_tracks":
                await self._conn.execute(
                    """
                    INSERT INTO added_tracks
                        (playlist_id, track_id, uri, title, artist, added_at, source_message_id, requester_id)
                    SELECT ?, track_id, uri, title, artist, added_at, source_message_id, requester_id
                    FROM added_tracks_legacy
                    """,
                    (playlist_id,),
                )
            else:
                await self._conn.execute(
                    """
                    INSERT INTO suppressed_tracks (playlist_id, track_id, removed_at, reason)
                    SELECT ?, track_id, removed_at, reason FROM suppressed_tracks_legacy
                    """,
                    (playlist_id,),
                )
            await self._conn.execute(f"DROP TABLE {table}_legacy")
            await self._conn.commit()
            logger.info("db.migrated_legacy_dedup_table", table=table, playlist_id=playlist_id)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "Database":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _maybe_commit(self) -> None:
        if self._autocommit:
            await self.conn.commit()

    async def checkpoint(self) -> None:
        """Flush pending writes without leaving batch mode - for periodic commits
        inside a long `batch()` block (e.g. every N messages of a /scan backfill)."""
        await self.conn.commit()

    @asynccontextmanager
    async def batch(self) -> AsyncIterator[None]:
        """Suppress the usual per-call commit for every write inside this block,
        committing once when it exits (or via manual checkpoint() calls within it).

        Built for /scan backfills, which otherwise commit once per processed link -
        a real fsync-round-trip per row over a channel history that can run into the
        thousands. Not used on the live-monitoring path, where each message's writes
        should land immediately.
        """
        previous = self._autocommit
        self._autocommit = False
        try:
            yield
        finally:
            self._autocommit = previous
            await self.conn.commit()

    # -- oauth tokens ---------------------------------------------------

    async def get_tokens(self) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT access_token_encrypted, refresh_token_encrypted, expires_at "
            "FROM oauth_tokens WHERE id = 1"
        )
        row = await cur.fetchone()
        await cur.close()
        return row

    async def save_tokens(
        self, access_token_encrypted: str | None, refresh_token_encrypted: str, expires_at: str
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO oauth_tokens (id, access_token_encrypted, refresh_token_encrypted, expires_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                access_token_encrypted = excluded.access_token_encrypted,
                refresh_token_encrypted = excluded.refresh_token_encrypted,
                expires_at = excluded.expires_at
            """,
            (access_token_encrypted, refresh_token_encrypted, expires_at),
        )
        await self._maybe_commit()

    # -- runtime config (e.g. per-guild playlist override from /playlist set) --

    async def get_config(self, key: str) -> str | None:
        cur = await self.conn.execute("SELECT value FROM runtime_config WHERE key = ?", (key,))
        row = await cur.fetchone()
        await cur.close()
        return row["value"] if row else None

    async def set_config(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO runtime_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._maybe_commit()

    # -- processed links (message-level dedupe) --------------------------
    # Keyed by Discord message ID, a globally-unique snowflake - no playlist/
    # guild scoping needed, a message can only ever belong to one guild.

    async def is_link_processed(self, message_id: str, link: str) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM processed_links WHERE message_id = ? AND link = ?",
            (message_id, link),
        )
        row = await cur.fetchone()
        await cur.close()
        return row is not None

    async def mark_link_processed(self, message_id: str, link: str) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO processed_links (message_id, link, created_at) VALUES (?, ?, ?)",
            (message_id, link, _now()),
        )
        await self._maybe_commit()

    # -- added tracks (track-level dedupe + attribution, per playlist) ---

    async def is_track_added(self, playlist_id: str, track_id: str) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM added_tracks WHERE playlist_id = ? AND track_id = ?", (playlist_id, track_id)
        )
        row = await cur.fetchone()
        await cur.close()
        return row is not None

    async def record_added_track(
        self,
        playlist_id: str,
        track_id: str,
        uri: str,
        title: str | None,
        artist: str | None,
        source_message_id: str | None,
        requester_id: str | None,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO added_tracks
                (playlist_id, track_id, uri, title, artist, added_at, source_message_id, requester_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(playlist_id, track_id) DO NOTHING
            """,
            (playlist_id, track_id, uri, title, artist, _now(), source_message_id, requester_id),
        )
        await self._maybe_commit()

    async def get_added_track_ids(self, playlist_id: str) -> set[str]:
        cur = await self.conn.execute(
            "SELECT track_id FROM added_tracks WHERE playlist_id = ?", (playlist_id,)
        )
        rows = await cur.fetchall()
        await cur.close()
        return {row["track_id"] for row in rows}

    async def remove_added_track(self, playlist_id: str, track_id: str) -> None:
        await self.conn.execute(
            "DELETE FROM added_tracks WHERE playlist_id = ? AND track_id = ?", (playlist_id, track_id)
        )
        await self._maybe_commit()

    async def count_added_tracks(self, playlist_id: str) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM added_tracks WHERE playlist_id = ?", (playlist_id,)
        )
        row = await cur.fetchone()
        await cur.close()
        return int(row["c"])

    # -- suppressed tracks (humans removed these; don't re-add), per playlist --

    async def is_suppressed(self, playlist_id: str, track_id: str) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM suppressed_tracks WHERE playlist_id = ? AND track_id = ?", (playlist_id, track_id)
        )
        row = await cur.fetchone()
        await cur.close()
        return row is not None

    async def suppress_track(self, playlist_id: str, track_id: str, reason: str = "removed_by_human") -> None:
        await self.conn.execute(
            """
            INSERT INTO suppressed_tracks (playlist_id, track_id, removed_at, reason) VALUES (?, ?, ?, ?)
            ON CONFLICT(playlist_id, track_id) DO UPDATE SET removed_at = excluded.removed_at, reason = excluded.reason
            """,
            (playlist_id, track_id, _now(), reason),
        )
        await self._maybe_commit()

    async def unsuppress_track(self, playlist_id: str, track_id: str) -> None:
        await self.conn.execute(
            "DELETE FROM suppressed_tracks WHERE playlist_id = ? AND track_id = ?", (playlist_id, track_id)
        )
        await self._maybe_commit()

    async def get_suppressed_ids(self, playlist_id: str) -> set[str]:
        cur = await self.conn.execute(
            "SELECT track_id FROM suppressed_tracks WHERE playlist_id = ?", (playlist_id,)
        )
        rows = await cur.fetchall()
        await cur.close()
        return {row["track_id"] for row in rows}

    # -- scan cursors (resumable backfill) --------------------------------
    # Keyed by Discord channel ID, a globally-unique snowflake - no scoping needed.

    async def get_cursor(self, channel_id: str) -> str | None:
        cur = await self.conn.execute(
            "SELECT last_message_id FROM scan_cursors WHERE channel_id = ?", (channel_id,)
        )
        row = await cur.fetchone()
        await cur.close()
        return row["last_message_id"] if row else None

    async def set_cursor(self, channel_id: str, last_message_id: str) -> None:
        await self.conn.execute(
            """
            INSERT INTO scan_cursors (channel_id, last_message_id, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                last_message_id = excluded.last_message_id, updated_at = excluded.updated_at
            """,
            (channel_id, last_message_id, _now()),
        )
        await self._maybe_commit()

    # -- odesli cache (permanent; the free tier is the tightest budget) --
    # Keyed by external source URL, independent of which playlist it ends up
    # feeding - no scoping needed.

    async def get_odesli_cache(self, source_url: str) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT spotify_url, title, artist, miss FROM odesli_cache WHERE source_url = ?",
            (source_url,),
        )
        row = await cur.fetchone()
        await cur.close()
        return row

    async def set_odesli_cache(
        self,
        source_url: str,
        spotify_url: str | None,
        title: str | None,
        artist: str | None,
        miss: bool,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO odesli_cache (source_url, spotify_url, title, artist, resolved_at, miss)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_url) DO UPDATE SET
                spotify_url = excluded.spotify_url,
                title = excluded.title,
                artist = excluded.artist,
                resolved_at = excluded.resolved_at,
                miss = excluded.miss
            """,
            (source_url, spotify_url, title, artist, _now(), int(miss)),
        )
        await self._maybe_commit()
