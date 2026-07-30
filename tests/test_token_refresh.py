from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from cratebot.config import Settings
from cratebot.crypto import TokenCipher
from cratebot.db import Database
from cratebot.spotify.auth import SpotifyAuth
from cratebot.spotify.errors import SpotifyAuthNotConfigured, SpotifyReauthRequired


TOKEN_URL = "https://accounts.spotify.com/api/token"


def _settings() -> Settings:
    return Settings(
        spotify_client_id="cid",
        spotify_client_secret="csecret",
        spotify_redirect_uri="http://127.0.0.1:8888/callback",
        token_encryption_key="",
    )


@pytest.fixture
def cipher(fernet_key: str) -> TokenCipher:
    return TokenCipher(fernet_key)


async def test_no_tokens_raises_not_configured(db: Database, cipher: TokenCipher) -> None:
    async with httpx.AsyncClient() as http:
        auth = SpotifyAuth(_settings(), db, cipher, http)
        with pytest.raises(SpotifyAuthNotConfigured):
            await auth.get_valid_access_token()


async def test_valid_unexpired_token_returned_without_refresh(db: Database, cipher: TokenCipher) -> None:
    refresh_after = datetime.now(timezone.utc) + timedelta(hours=1)
    await db.save_tokens(
        cipher.encrypt("current-access-token"), cipher.encrypt("current-refresh-token"), refresh_after.isoformat()
    )
    async with httpx.AsyncClient() as http:
        auth = SpotifyAuth(_settings(), db, cipher, http)
        token = await auth.get_valid_access_token()
    assert token == "current-access-token"


@respx.mock
async def test_expired_token_triggers_refresh_with_new_refresh_token(db: Database, cipher: TokenCipher) -> None:
    refresh_after = datetime.now(timezone.utc) - timedelta(seconds=1)  # already past the trigger
    await db.save_tokens(cipher.encrypt("old-access"), cipher.encrypt("old-refresh"), refresh_after.isoformat())

    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600},
        )
    )

    async with httpx.AsyncClient() as http:
        auth = SpotifyAuth(_settings(), db, cipher, http)
        token = await auth.get_valid_access_token()

    assert token == "new-access"
    row = await db.get_tokens()
    assert cipher.decrypt(row["refresh_token_encrypted"]) == "new-refresh"


@respx.mock
async def test_refresh_without_new_refresh_token_keeps_old_one(db: Database, cipher: TokenCipher) -> None:
    refresh_after = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.save_tokens(cipher.encrypt("old-access"), cipher.encrypt("keep-me-refresh"), refresh_after.isoformat())

    # Spotify's response omits refresh_token entirely - the current one must be kept, not discarded.
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "new-access", "expires_in": 3600})
    )

    async with httpx.AsyncClient() as http:
        auth = SpotifyAuth(_settings(), db, cipher, http)
        token = await auth.get_valid_access_token()

    assert token == "new-access"
    row = await db.get_tokens()
    assert cipher.decrypt(row["refresh_token_encrypted"]) == "keep-me-refresh"


@respx.mock
async def test_invalid_grant_raises_reauth_required(db: Database, cipher: TokenCipher) -> None:
    refresh_after = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.save_tokens(cipher.encrypt("old-access"), cipher.encrypt("revoked-refresh"), refresh_after.isoformat())

    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant", "error_description": "revoked"})
    )

    async with httpx.AsyncClient() as http:
        auth = SpotifyAuth(_settings(), db, cipher, http)
        with pytest.raises(SpotifyReauthRequired):
            await auth.get_valid_access_token()


@respx.mock
async def test_proactive_refresh_triggers_before_hard_expiry(db: Database, cipher: TokenCipher) -> None:
    # refresh_after represents the 80%-lifetime trigger, already passed, even though a naive
    # "is it hard-expired yet" check would still say the token is fine.
    refresh_after = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.save_tokens(cipher.encrypt("soon-to-be-stale"), cipher.encrypt("refresh-tok"), refresh_after.isoformat())

    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "proactively-refreshed", "refresh_token": "r2", "expires_in": 3600}
        )
    )

    async with httpx.AsyncClient() as http:
        auth = SpotifyAuth(_settings(), db, cipher, http)
        token = await auth.get_valid_access_token()

    assert token == "proactively-refreshed"
    assert route.call_count == 1
