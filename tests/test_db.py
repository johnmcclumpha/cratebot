from __future__ import annotations

import pytest

from cratebot.db import Database



async def test_processed_links_idempotent(db: Database) -> None:
    assert not await db.is_link_processed("msg1", "https://open.spotify.com/track/abc")
    await db.mark_link_processed("msg1", "https://open.spotify.com/track/abc")
    assert await db.is_link_processed("msg1", "https://open.spotify.com/track/abc")
    # re-marking is idempotent, not an error
    await db.mark_link_processed("msg1", "https://open.spotify.com/track/abc")
    # a different message with the same link is a distinct key
    assert not await db.is_link_processed("msg2", "https://open.spotify.com/track/abc")


async def test_added_tracks_dedupe(db: Database) -> None:
    assert not await db.is_track_added("pl1", "trackid1")
    await db.record_added_track("pl1", "trackid1", "spotify:track:trackid1", "Title", "Artist", "msg1", "user1")
    assert await db.is_track_added("pl1", "trackid1")
    assert await db.count_added_tracks("pl1") == 1
    # inserting again is a no-op (ON CONFLICT DO NOTHING)
    await db.record_added_track("pl1", "trackid1", "spotify:track:trackid1", "Title", "Artist", "msg2", "user2")
    assert await db.count_added_tracks("pl1") == 1
    assert await db.get_added_track_ids("pl1") == {"trackid1"}


async def test_added_tracks_scoped_per_playlist(db: Database) -> None:
    """The whole point of multi-guild support: a track added to one playlist
    must not be blocked from also being added to a different one."""
    await db.record_added_track("pl1", "trackid1", "spotify:track:trackid1", "Title", "Artist", "msg1", "user1")
    assert await db.is_track_added("pl1", "trackid1")
    assert not await db.is_track_added("pl2", "trackid1")
    assert await db.count_added_tracks("pl1") == 1
    assert await db.count_added_tracks("pl2") == 0

    await db.record_added_track("pl2", "trackid1", "spotify:track:trackid1", "Title", "Artist", "msg2", "user2")
    assert await db.is_track_added("pl2", "trackid1")
    assert await db.count_added_tracks("pl1") == 1
    assert await db.count_added_tracks("pl2") == 1


async def test_suppressed_tracks_prevent_readd(db: Database) -> None:
    await db.record_added_track("pl1", "trackid1", "spotify:track:trackid1", "Title", "Artist", "msg1", "user1")
    assert not await db.is_suppressed("pl1", "trackid1")
    await db.suppress_track("pl1", "trackid1", reason="removed_by_human")
    await db.remove_added_track("pl1", "trackid1")
    assert await db.is_suppressed("pl1", "trackid1")
    assert not await db.is_track_added("pl1", "trackid1")
    assert await db.get_suppressed_ids("pl1") == {"trackid1"}

    await db.unsuppress_track("pl1", "trackid1")
    assert not await db.is_suppressed("pl1", "trackid1")


async def test_suppressed_tracks_scoped_per_playlist(db: Database) -> None:
    """A human removing a track from one guild's playlist must not suppress it
    from ever being added to a different guild's playlist."""
    await db.suppress_track("pl1", "trackid1", reason="removed_by_human")
    assert await db.is_suppressed("pl1", "trackid1")
    assert not await db.is_suppressed("pl2", "trackid1")


async def test_scan_cursor_resumable(db: Database) -> None:
    assert await db.get_cursor("chan1") is None
    await db.set_cursor("chan1", "1000")
    assert await db.get_cursor("chan1") == "1000"
    await db.set_cursor("chan1", "2000")
    assert await db.get_cursor("chan1") == "2000"


async def test_odesli_cache_hit_and_miss(db: Database) -> None:
    assert await db.get_odesli_cache("https://youtu.be/x") is None
    await db.set_odesli_cache(
        "https://youtu.be/x", "https://open.spotify.com/track/abc", "Title", "Artist", miss=False
    )
    row = await db.get_odesli_cache("https://youtu.be/x")
    assert row is not None
    assert row["spotify_url"] == "https://open.spotify.com/track/abc"
    assert row["miss"] == 0

    await db.set_odesli_cache("https://youtu.be/y", None, None, None, miss=True)
    row = await db.get_odesli_cache("https://youtu.be/y")
    assert row is not None
    assert row["miss"] == 1


async def test_runtime_config_roundtrip(db: Database) -> None:
    assert await db.get_config("playlist_id") is None
    await db.set_config("playlist_id", "abc123")
    assert await db.get_config("playlist_id") == "abc123"
    await db.set_config("playlist_id", "def456")
    assert await db.get_config("playlist_id") == "def456"


async def test_batch_defers_commit_until_exit(tmp_path) -> None:
    path = str(tmp_path / "batch.db")
    writer = Database(path)
    await writer.connect()
    reader = Database(path)
    await reader.connect()
    try:
        async with writer.batch():
            await writer.mark_link_processed("msg1", "https://open.spotify.com/track/abc")
            # uncommitted - a separate connection must not see it yet
            assert not await reader.is_link_processed("msg1", "https://open.spotify.com/track/abc")
        # batch() commits once on exit
        assert await reader.is_link_processed("msg1", "https://open.spotify.com/track/abc")
    finally:
        await writer.close()
        await reader.close()


async def test_batch_checkpoint_commits_without_leaving_batch_mode(tmp_path) -> None:
    path = str(tmp_path / "checkpoint.db")
    writer = Database(path)
    await writer.connect()
    reader = Database(path)
    await reader.connect()
    try:
        async with writer.batch():
            await writer.mark_link_processed("msg1", "https://open.spotify.com/track/abc")
            await writer.checkpoint()
            assert await reader.is_link_processed("msg1", "https://open.spotify.com/track/abc")

            # still inside batch() - the next write stays uncommitted until the next
            # checkpoint/exit, proving checkpoint() didn't turn autocommit back on
            await writer.mark_link_processed("msg2", "https://open.spotify.com/track/abc")
            assert not await reader.is_link_processed("msg2", "https://open.spotify.com/track/abc")
        assert await reader.is_link_processed("msg2", "https://open.spotify.com/track/abc")
    finally:
        await writer.close()
        await reader.close()


async def test_legacy_dedup_tables_migrate_and_backfill_playlist_id(tmp_path) -> None:
    """Pre-multi-guild databases have added_tracks/suppressed_tracks with no
    playlist_id column at all (the bot only ever supported one playlist).
    connect() must detect that shape, backfill every existing row with the
    resolved playlist ID, and be idempotent on a second connect."""
    import aiosqlite

    path = str(tmp_path / "legacy.db")
    conn = await aiosqlite.connect(path)
    await conn.execute(
        """
        CREATE TABLE added_tracks (
            track_id TEXT PRIMARY KEY,
            uri TEXT NOT NULL,
            title TEXT,
            artist TEXT,
            added_at TEXT NOT NULL,
            source_message_id TEXT,
            requester_id TEXT
        )
        """
    )
    await conn.execute(
        "CREATE TABLE suppressed_tracks (track_id TEXT PRIMARY KEY, removed_at TEXT NOT NULL, reason TEXT)"
    )
    await conn.execute(
        "INSERT INTO added_tracks VALUES ('t1', 'spotify:track:t1', 'Title1', 'Artist1', '2026-01-01T00:00:00+00:00', 'msg1', 'user1')"
    )
    await conn.execute(
        "INSERT INTO added_tracks VALUES ('t2', 'spotify:track:t2', 'Title2', 'Artist2', '2026-01-01T00:00:00+00:00', 'msg2', 'user2')"
    )
    await conn.execute("INSERT INTO suppressed_tracks VALUES ('t3', '2026-01-01T00:00:00+00:00', 'removed_by_human')")
    await conn.commit()
    await conn.close()

    db = Database(path)
    await db.connect(fallback_playlist_id="legacy-playlist")
    try:
        assert await db.is_track_added("legacy-playlist", "t1")
        assert await db.is_track_added("legacy-playlist", "t2")
        assert await db.is_suppressed("legacy-playlist", "t3")
        assert await db.count_added_tracks("legacy-playlist") == 2
    finally:
        await db.close()

    # idempotent: reconnecting must not error or re-migrate
    db2 = Database(path)
    await db2.connect(fallback_playlist_id="legacy-playlist")
    try:
        assert await db2.count_added_tracks("legacy-playlist") == 2
    finally:
        await db2.close()


async def test_legacy_migration_raises_without_a_resolvable_playlist_id(tmp_path) -> None:
    """Migrating an old-schema table with nothing to backfill playlist_id from
    would silently orphan existing dedup history - must fail loudly instead."""
    import aiosqlite

    path = str(tmp_path / "legacy_no_fallback.db")
    conn = await aiosqlite.connect(path)
    await conn.execute(
        "CREATE TABLE added_tracks (track_id TEXT PRIMARY KEY, uri TEXT NOT NULL, title TEXT, artist TEXT, "
        "added_at TEXT NOT NULL, source_message_id TEXT, requester_id TEXT)"
    )
    await conn.execute(
        "INSERT INTO added_tracks VALUES ('t1', 'spotify:track:t1', 'Title1', 'Artist1', '2026-01-01T00:00:00+00:00', 'msg1', 'user1')"
    )
    await conn.commit()
    await conn.close()

    db = Database(path)
    with pytest.raises(RuntimeError):
        await db.connect(fallback_playlist_id=None)
    await db.close()


async def test_oauth_tokens_roundtrip(db: Database) -> None:
    assert await db.get_tokens() is None
    await db.save_tokens("enc-access", "enc-refresh", "2026-01-01T00:00:00+00:00")
    row = await db.get_tokens()
    assert row is not None
    assert row["access_token_encrypted"] == "enc-access"
    assert row["refresh_token_encrypted"] == "enc-refresh"
    await db.save_tokens(None, "enc-refresh-2", "2026-02-01T00:00:00+00:00")
    row = await db.get_tokens()
    assert row["access_token_encrypted"] is None
    assert row["refresh_token_encrypted"] == "enc-refresh-2"
