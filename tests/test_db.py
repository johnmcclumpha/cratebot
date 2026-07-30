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
    assert not await db.is_track_added("trackid1")
    await db.record_added_track("trackid1", "spotify:track:trackid1", "Title", "Artist", "msg1", "user1")
    assert await db.is_track_added("trackid1")
    assert await db.count_added_tracks() == 1
    # inserting again is a no-op (ON CONFLICT DO NOTHING)
    await db.record_added_track("trackid1", "spotify:track:trackid1", "Title", "Artist", "msg2", "user2")
    assert await db.count_added_tracks() == 1
    assert await db.get_added_track_ids() == {"trackid1"}


async def test_suppressed_tracks_prevent_readd(db: Database) -> None:
    await db.record_added_track("trackid1", "spotify:track:trackid1", "Title", "Artist", "msg1", "user1")
    assert not await db.is_suppressed("trackid1")
    await db.suppress_track("trackid1", reason="removed_by_human")
    await db.remove_added_track("trackid1")
    assert await db.is_suppressed("trackid1")
    assert not await db.is_track_added("trackid1")
    assert await db.get_suppressed_ids() == {"trackid1"}

    await db.unsuppress_track("trackid1")
    assert not await db.is_suppressed("trackid1")


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
