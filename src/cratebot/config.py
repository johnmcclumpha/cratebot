"""Runtime configuration, loaded from environment variables / .env."""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class GuildConfig(BaseModel):
    """Per-Discord-server config: which channels to watch and which Spotify
    playlist they file into. One entry per server in the GUILDS env var."""

    guild_id: int
    monitored_channel_ids: list[int] = Field(default_factory=list)
    spotify_playlist_id: str
    admin_role_ids: list[int] = Field(default_factory=list)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Discord
    discord_bot_token: SecretStr = SecretStr("")
    guilds: list[GuildConfig] = Field(default_factory=list)

    # Spotify
    spotify_client_id: str = ""
    spotify_client_secret: SecretStr = SecretStr("")
    spotify_redirect_uri: str = "http://127.0.0.1:8888/callback"

    # Odesli
    odesli_api_key: SecretStr = SecretStr("")

    # Security
    token_encryption_key: SecretStr = SecretStr("")

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

    @field_validator("guilds")
    @classmethod
    def _unique_guild_ids(cls, value: list[GuildConfig]) -> list[GuildConfig]:
        seen: set[int] = set()
        for guild in value:
            if guild.guild_id in seen:
                raise ValueError(f"Duplicate guild_id {guild.guild_id} in GUILDS - each server needs its own entry.")
            seen.add(guild.guild_id)
        return value

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
