from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from svu.cli import EXIT_CONFIG, main
from svu.config import ConfigError, Settings, format_validation_error

PASSWORD = "db-password-123"


def test_defaults_point_at_the_compose_services(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SVU_DB_PASSWORD", PASSWORD)
    settings = Settings()
    assert settings.db_host == "postgres"
    assert settings.vc_base_url == "https://vcrypto.sumidora.com"
    assert settings.database_url.drivername == "postgresql+asyncpg"
    assert PASSWORD not in repr(settings.database_url)


def test_unknown_variables_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SVU_DB_PASSWORD", PASSWORD)
    monkeypatch.setenv("SVU_DISCORD_TOKN", "typo")
    with pytest.raises(ConfigError, match="SVU_DISCORD_TOKN"):
        Settings()


def test_unknown_variables_in_the_env_file_are_rejected(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(f"SVU_DB_PASSWORD={PASSWORD}\nSVU_NOT_A_SETTING=1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="SVU_NOT_A_SETTING"):
        Settings(_env_file=env)


def test_test_variables_are_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SVU_DB_PASSWORD", PASSWORD)
    monkeypatch.setenv("SVU_TEST_DB_URL", "postgresql+asyncpg://x@y/z_test")
    Settings()


def test_admin_ids_are_parsed_and_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SVU_DB_PASSWORD", PASSWORD)
    monkeypatch.setenv("SVU_ADMIN_IDS", " 1, 22 ,333 ")
    assert Settings().admin_ids == [1, 22, 333]
    monkeypatch.setenv("SVU_ADMIN_IDS", "1,abc")
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.setenv("SVU_ADMIN_IDS", "1,1")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("value", ["http://svu.example.com", "https://svu.example.com/path", "svu.example.com", "https://svu.example.com/?x=1"])
def test_public_base_url_must_be_an_https_origin(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SVU_DB_PASSWORD", PASSWORD)
    monkeypatch.setenv("SVU_PUBLIC_BASE_URL", value)
    with pytest.raises(ValidationError):
        Settings()


def test_public_base_url_loses_its_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SVU_DB_PASSWORD", PASSWORD)
    monkeypatch.setenv("SVU_PUBLIC_BASE_URL", "https://svu.example.com/")
    assert Settings().public_base_url == "https://svu.example.com"


def test_blank_optional_values_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SVU_DB_PASSWORD", PASSWORD)
    monkeypatch.setenv("SVU_DISCORD_TOKEN", "  ")
    monkeypatch.setenv("SVU_DISCORD_CLIENT_ID", "")
    settings = Settings()
    assert settings.discord_token is None
    assert settings.discord_client_id is None


def test_secret_key_has_a_minimum_length(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SVU_DB_PASSWORD", PASSWORD)
    monkeypatch.setenv("SVU_SECRET_KEY", "short")
    with pytest.raises(ValidationError):
        Settings()


def test_require_names_every_missing_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SVU_DB_PASSWORD", PASSWORD)
    settings = Settings()
    with pytest.raises(ConfigError) as info:
        settings.require("discord_token", "vc_client_id")
    assert "SVU_DISCORD_TOKEN" in str(info.value) and "SVU_VC_CLIENT_ID" in str(info.value)


def test_validation_errors_name_variables_but_not_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SVU_DB_PASSWORD", "x")
    with pytest.raises(ValidationError) as info:
        Settings()
    text = format_validation_error(info.value)
    assert "SVU_DB_PASSWORD" in text
    assert "'x'" not in text


def test_secret_values_cover_every_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SVU_DB_PASSWORD", PASSWORD)
    monkeypatch.setenv("SVU_DISCORD_TOKEN", "discord-token-value")
    monkeypatch.setenv("SVU_SECRET_KEY", "k" * 40)
    monkeypatch.setenv("TUNNEL_TOKEN", "tunnel-token-value")
    values = Settings().secret_values()
    assert {PASSWORD, "discord-token-value", "k" * 40, "tunnel-token-value"} <= set(values)


def test_cli_exits_78_on_missing_password(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["migrate"]) == EXIT_CONFIG
    assert "SVU_DB_PASSWORD" in capsys.readouterr().err


def test_cli_exits_78_when_a_role_lacks_its_settings(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("SVU_DB_PASSWORD", PASSWORD)
    assert main(["worker"]) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "SVU_DISCORD_TOKEN" in err and "SVU_SECRET_KEY" in err
