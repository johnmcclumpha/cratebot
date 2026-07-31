from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from cratebot.config import GuildConfig, Settings


def test_guilds_defaults_to_empty_list_when_unset() -> None:
    settings = Settings(_env_file=None)
    assert settings.guilds == []


def test_guilds_env_var_parses_valid_json_array(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "GUILDS",
        '[{"guild_id": 111, "monitored_channel_ids": [222, 333], '
        '"spotify_playlist_id": "pl1", "admin_role_ids": [444]}]',
    )
    settings = Settings(_env_file=None)
    assert settings.guilds == [
        GuildConfig(guild_id=111, monitored_channel_ids=[222, 333], spotify_playlist_id="pl1", admin_role_ids=[444])
    ]


def test_guilds_env_var_parses_multiple_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "GUILDS",
        '[{"guild_id": 1, "spotify_playlist_id": "pl1"}, {"guild_id": 2, "spotify_playlist_id": "pl2"}]',
    )
    settings = Settings(_env_file=None)
    assert len(settings.guilds) == 2
    assert {g.guild_id for g in settings.guilds} == {1, 2}
    # optional fields default sensibly when omitted from the JSON
    assert settings.guilds[0].monitored_channel_ids == []
    assert settings.guilds[0].admin_role_ids == []


def test_duplicate_guild_id_raises() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            guilds=[
                {"guild_id": 1, "spotify_playlist_id": "pl1"},
                {"guild_id": 1, "spotify_playlist_id": "pl2"},
            ],
        )


def test_secrets_round_trip_as_secretstr() -> None:
    settings = Settings(
        _env_file=None,
        discord_bot_token="tok",
        spotify_client_secret="secret",
        token_encryption_key="key",
        odesli_api_key="odesli-key",
    )
    assert isinstance(settings.discord_bot_token, SecretStr)
    assert settings.discord_bot_token.get_secret_value() == "tok"
    assert isinstance(settings.spotify_client_secret, SecretStr)
    assert settings.spotify_client_secret.get_secret_value() == "secret"
    assert isinstance(settings.token_encryption_key, SecretStr)
    assert settings.token_encryption_key.get_secret_value() == "key"
    assert isinstance(settings.odesli_api_key, SecretStr)
    assert settings.odesli_api_key.get_secret_value() == "odesli-key"
    # never leak the raw value via repr/str
    assert "tok" not in repr(settings.discord_bot_token)
    assert "secret" not in repr(settings.spotify_client_secret)
