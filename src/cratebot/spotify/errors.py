"""Spotify-specific exceptions."""

from __future__ import annotations


class SpotifyError(Exception):
    """Base class for all Spotify-related failures."""


class SpotifyAPIError(SpotifyError):
    def __init__(self, status_code: int, message: str, payload: dict | None = None) -> None:
        super().__init__(f"Spotify API error {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.payload = payload or {}


class SpotifyQuotaExceededError(SpotifyError):
    """Dev Mode quota ceiling hit (reason=QUOTA_EXCEEDED). Retrying will not help."""


class SpotifyAuthNotConfigured(SpotifyError):
    """No tokens on file; the CLI setup flow (cratebot-auth) hasn't been run yet."""


class SpotifyReauthRequired(SpotifyError):
    """Refresh token was rejected (revoked / lapsed Premium). Full re-auth is needed."""


class PlaylistNotOwnedError(SpotifyError):
    """The authorising account does not own the target playlist (see brief 2.1)."""

    def __init__(self, playlist_id: str, owner_id: str, authorised_user_id: str) -> None:
        super().__init__(
            f"Playlist {playlist_id} is owned by '{owner_id}', not the authorising account "
            f"'{authorised_user_id}'. Spotify does not allow adding tracks to a playlist you "
            "don't own, even as a collaborator. Use a playlist owned by the account that "
            "completed OAuth consent."
        )
        self.playlist_id = playlist_id
        self.owner_id = owner_id
        self.authorised_user_id = authorised_user_id
