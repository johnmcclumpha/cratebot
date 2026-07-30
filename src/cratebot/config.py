"""Runtime configuration, loaded from environment variables / .env."""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv_ints(value: str | list[int]) -> list[int]:
    if isinstance(value, list):
        return value
    value = value.strip()
    if not value:
        return []
    return [int(part.strip()) for part in value.split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Discord
    discord_bot_token: str = ""
    discord_guild_id: int = 0
    monitored_channel_ids: list[int] = Field(default_factory=list)
    admin_role_ids: list[int] = Field(default_factory=list)

    # Spotify
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = "http://127.0.0.1:8888/callback"
    spotify_playlist_id: str = ""

    # Odesli
    odesli_api_key: str = ""

    # Security
    token_encryption_key: str = ""

    # Behaviour
    expand_albums: bool = False
    expand_episodes: bool = False
    allow_bot_authors: bool = False
    match_threshold: float = 0.85
    scan_strategy: str = "search"
    dry_run_default: bool = True

    # Storage / logging
    database_path: str = "./data/cratebot.db"
    log_level: str = "INFO"

    @field_validator("monitored_channel_ids", "admin_role_ids", mode="before")
    @classmethod
    def _parse_csv_ints(cls, value: str | list[int]) -> list[int]:
        return _split_csv_ints(value)

    @property
    def spotify_scopes(self) -> str:
        return " ".join(
            [
                "playlist-modify-public",
                "playlist-modify-private",
                "playlist-read-private",
                "playlist-read-collaborative",
            ]
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
