"""Spotify Authorization Code flow: one-time CLI setup + proactive token refresh.

Confidential server flow (client secret, not PKCE) per brief section 7.
The refresh-token state machine has one sharp edge worth re-reading before
touching this file: a refresh response may or may not include a new
refresh_token. If it's present, the old one must be discarded. If it's
absent, the *current* refresh token remains valid and must be kept. Getting
this backwards kills the bot at an unpredictable point weeks later.
"""

from __future__ import annotations

import asyncio
import base64
import secrets
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from cratebot.config import Settings, get_settings
from cratebot.crypto import TokenCipher
from cratebot.db import Database
from cratebot.logging_setup import get_logger
from cratebot.spotify.errors import SpotifyAuthNotConfigured, SpotifyReauthRequired

logger = get_logger(__name__)

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

# Refresh once this fraction of the access token's lifetime has elapsed.
PROACTIVE_REFRESH_FRACTION = 0.8


class SpotifyAuth:
    """Loads/refreshes tokens for the long-running bot process."""

    def __init__(self, settings: Settings, db: Database, cipher: TokenCipher, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._db = db
        self._cipher = cipher
        self._http = http
        self._lock = asyncio.Lock()
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._refresh_after: datetime | None = None

    async def _load_from_db(self) -> None:
        row = await self._db.get_tokens()
        if row is None:
            raise SpotifyAuthNotConfigured(
                "No Spotify tokens on file. Run `cratebot-auth` to complete the one-time OAuth setup."
            )
        self._access_token = (
            self._cipher.decrypt(row["access_token_encrypted"]) if row["access_token_encrypted"] else None
        )
        self._refresh_token = self._cipher.decrypt(row["refresh_token_encrypted"])
        self._refresh_after = datetime.fromisoformat(row["expires_at"])

    async def get_valid_access_token(self) -> str:
        async with self._lock:
            if self._refresh_token is None:
                await self._load_from_db()
            assert self._refresh_after is not None
            if self._access_token is None or datetime.now(timezone.utc) >= self._refresh_after:
                await self._refresh_locked()
            assert self._access_token is not None
            return self._access_token

    async def force_refresh(self) -> str:
        async with self._lock:
            if self._refresh_token is None:
                await self._load_from_db()
            await self._refresh_locked()
            assert self._access_token is not None
            return self._access_token

    async def _refresh_locked(self) -> None:
        assert self._refresh_token is not None
        response = await self._http.post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": self._refresh_token},
            headers=_basic_auth_header(self._settings.spotify_client_id, self._settings.spotify_client_secret),
        )
        if response.status_code == 400:
            body = _safe_json(response)
            if body.get("error") == "invalid_grant":
                raise SpotifyReauthRequired(
                    "Spotify rejected the refresh token (revoked, or Premium lapsed). "
                    "Re-run `cratebot-auth`."
                )
        response.raise_for_status()
        payload = response.json()

        new_access_token = payload["access_token"]
        expires_in = payload["expires_in"]
        # Only overwrite the refresh token if Spotify actually sent a new one.
        new_refresh_token = payload.get("refresh_token") or self._refresh_token

        refresh_after = datetime.now(timezone.utc) + timedelta(
            seconds=expires_in * PROACTIVE_REFRESH_FRACTION
        )
        await self._db.save_tokens(
            self._cipher.encrypt(new_access_token),
            self._cipher.encrypt(new_refresh_token),
            refresh_after.isoformat(),
        )
        self._access_token = new_access_token
        self._refresh_token = new_refresh_token
        self._refresh_after = refresh_after
        logger.info("spotify.token_refreshed", refresh_after=refresh_after.isoformat())


def _basic_auth_header(client_id: str, client_secret: str) -> dict[str, str]:
    raw = f"{client_id}:{client_secret}".encode()
    return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}


def _safe_json(response: httpx.Response) -> dict:
    try:
        return response.json()
    except ValueError:
        return {}


def build_authorize_url(settings: Settings, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.spotify_client_id,
        "scope": settings.spotify_scopes,
        "redirect_uri": settings.spotify_redirect_uri,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


class _CallbackResult:
    code: str | None = None
    state: str | None = None
    error: str | None = None


def _run_local_callback_server(redirect_uri: str, expected_state: str, timeout: float = 300.0) -> _CallbackResult:
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8888
    result = _CallbackResult()
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (stdlib signature)
            query = parse_qs(urlparse(self.path).query)
            result.code = query.get("code", [None])[0]
            result.state = query.get("state", [None])[0]
            result.error = query.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            message = (
                b"<html><body><h3>Cratebot: authorisation received, you can close this tab.</h3></body></html>"
                if result.code
                else b"<html><body><h3>Cratebot: authorisation failed, check the terminal.</h3></body></html>"
            )
            self.wfile.write(message)
            done.set()

        def log_message(self, format: str, *args: object) -> None:  # silence default stderr logging
            pass

    server = HTTPServer((host, port), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        if not done.wait(timeout=timeout):
            raise TimeoutError("Timed out waiting for the Spotify OAuth redirect.")
    finally:
        server.shutdown()
        server_thread.join()

    if result.error:
        raise RuntimeError(f"Spotify authorisation error: {result.error}")
    if result.state != expected_state:
        raise RuntimeError("OAuth state mismatch; possible CSRF, aborting.")
    if not result.code:
        raise RuntimeError("No authorization code received.")
    return result


async def _exchange_code_and_persist(settings: Settings, code: str) -> None:
    async with httpx.AsyncClient(timeout=15.0) as http:
        response = await http.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.spotify_redirect_uri,
            },
            headers=_basic_auth_header(settings.spotify_client_id, settings.spotify_client_secret),
        )
        response.raise_for_status()
        payload = response.json()

    cipher = TokenCipher(settings.token_encryption_key)
    refresh_after = datetime.now(timezone.utc) + timedelta(
        seconds=payload["expires_in"] * PROACTIVE_REFRESH_FRACTION
    )
    db = Database(settings.database_path)
    await db.connect()
    try:
        await db.save_tokens(
            cipher.encrypt(payload["access_token"]),
            cipher.encrypt(payload["refresh_token"]),
            refresh_after.isoformat(),
        )
    finally:
        await db.close()


def cli_setup() -> None:
    """Entry point for `cratebot-auth`: interactive one-time OAuth consent."""
    settings = get_settings()
    if not settings.spotify_client_id or not settings.spotify_client_secret:
        raise SystemExit("SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET must be set in .env before running this.")
    if not settings.token_encryption_key:
        raise SystemExit(
            "TOKEN_ENCRYPTION_KEY must be set in .env before running this. Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )

    state = secrets.token_urlsafe(24)
    url = build_authorize_url(settings, state)

    print("Log in with the Spotify account that should OWN the bot's playlist.")
    print("Opening your browser for Spotify authorisation. If it doesn't open, visit:\n")
    print(url, "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    result = _run_local_callback_server(settings.spotify_redirect_uri, state)
    assert result.code is not None
    asyncio.run(_exchange_code_and_persist(settings, result.code))
    print("Spotify authorisation complete. Tokens stored (encrypted) in the database.")
    print("Next: run `/playlist set <playlist_url>` in Discord, or set SPOTIFY_PLAYLIST_ID in .env.")


if __name__ == "__main__":
    cli_setup()
