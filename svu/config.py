"""Runtime settings read from SVU_* environment variables (and .env), validated at construction."""

from __future__ import annotations

import os
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy import URL

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

ENV_PREFIX = "SVU_"

# Variables under this prefix configure the test suite, not the application.
TEST_ENV_PREFIX = "SVU_TEST_"

# Discord ids are snowflakes stored in signed BIGINT columns.
ID_MAX = 2**63 - 1

PASSWORD_MIN_LENGTH = 8
SECRET_KEY_MIN_LENGTH = 32

# Infrastructure secrets that are redacted from logs whenever they are present in the process environment.
FOREIGN_SECRET_ENV_VARS = ("POSTGRES_PASSWORD", "TUNNEL_TOKEN")

_UNSET: Any = object()


class ConfigError(Exception):
    """A setting is missing or invalid for the role being started."""


def _env_file_names(env_file: object) -> list[str]:
    if env_file is None:
        return []
    files: Iterable[object] = [env_file] if isinstance(env_file, str | os.PathLike) else env_file  # type: ignore[assignment]
    names: list[str] = []
    for path in files:
        if isinstance(path, str | os.PathLike) and Path(path).is_file():
            names.extend(dotenv_values(path).keys())
    return names


def _https_base_url(value: str, *, allow_http: bool) -> str:
    parts = urlsplit(value)
    schemes = {"https", "http"} if allow_http else {"https"}
    if parts.scheme not in schemes or not parts.netloc or parts.query or parts.fragment:
        raise ValueError(f"must be an absolute {' or '.join(sorted(schemes))} URL without query or fragment")
    return value.rstrip("/")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_default=True,
        hide_input_in_errors=True,
    )

    db_host: str = Field(default="postgres", min_length=1)
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = Field(default="svu", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
    db_user: str = Field(default="svu", min_length=1)
    db_password: SecretStr = Field(min_length=PASSWORD_MIN_LENGTH)

    discord_token: SecretStr | None = None
    discord_client_id: int | None = Field(default=None, ge=1, le=ID_MAX)
    discord_client_secret: SecretStr | None = None

    vc_base_url: str = "https://vcrypto.sumidora.com"
    vc_client_id: str | None = None
    vc_client_secret: SecretStr | None = None

    public_base_url: str | None = None
    secret_key: SecretStr | None = Field(default=None, min_length=SECRET_KEY_MIN_LENGTH)

    admin_ids: Annotated[list[Annotated[int, Field(ge=1, le=ID_MAX)]], NoDecode] = Field(default_factory=list)

    claim_timeout_seconds: int = Field(default=600, ge=60, le=86400)

    backup_dir: Path = Path("/var/lib/svu/backups")
    backup_retention: int = Field(default=14, ge=1)
    backup_interval_seconds: int = Field(default=86400, ge=3600)

    api_host: str = "0.0.0.0"  # noqa: S104 - the API listens on all interfaces inside its container.
    api_port: int = Field(default=8000, ge=1, le=65535)

    log_level: LogLevel = "INFO"

    def __init__(self, _env_file: Any = _UNSET, **values: Any) -> None:
        """Reject unknown keyword arguments and unknown SVU_* names in the environment or the env file with ConfigError, then load and validate."""
        unknown_kwargs = sorted(name for name in values if not name.startswith("_") and name not in type(self).model_fields)
        if unknown_kwargs:
            raise ConfigError(f"unknown settings: {', '.join(unknown_kwargs)}")
        env_file = self.model_config.get("env_file") if _env_file is _UNSET else _env_file
        self._reject_unknown_variables([*os.environ, *_env_file_names(env_file)])
        if _env_file is _UNSET:
            super().__init__(**values)
        else:
            super().__init__(_env_file=_env_file, **values)

    @classmethod
    def known_variables(cls) -> frozenset[str]:
        return frozenset(f"{ENV_PREFIX}{name}".upper() for name in cls.model_fields)

    @classmethod
    def _reject_unknown_variables(cls, names: Iterable[str]) -> None:
        known = cls.known_variables()
        unknown = sorted({name.upper() for name in names if name.upper().startswith(ENV_PREFIX) and not name.upper().startswith(TEST_ENV_PREFIX)} - known)
        if unknown:
            raise ConfigError(f"unknown settings variables: {', '.join(unknown)}")

    @field_validator("db_password")
    @classmethod
    def _password_not_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("discord_token", "discord_client_secret", "vc_client_id", "vc_client_secret", "public_base_url", "secret_key", "discord_client_id", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("vc_base_url")
    @classmethod
    def _vc_base_url(cls, value: str) -> str:
        return _https_base_url(value, allow_http=True)

    @field_validator("public_base_url")
    @classmethod
    def _public_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _https_base_url(value, allow_http=False)
        if urlsplit(normalized).path not in {"", "/"}:
            raise ValueError("must be an origin without a path, such as https://svu.example.com")
        return normalized

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        ids: list[int] = []
        for part in value.split(","):
            item = part.strip()
            if not item:
                continue
            if not item.isascii() or not item.isdigit():
                raise ValueError("must be a comma-separated list of Discord user ids")
            ids.append(int(item))
        return ids

    @field_validator("admin_ids")
    @classmethod
    def _admin_ids_unique(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("user ids must not repeat")
        return value

    @field_validator("backup_dir")
    @classmethod
    def _backup_dir_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("must be an absolute path")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @property
    def database_url(self) -> URL:
        """SQLAlchemy URL for the asyncpg driver; its repr and str() mask the password."""
        return URL.create(
            "postgresql+asyncpg",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )

    def require(self, *names: str) -> None:
        """Raise ConfigError naming every listed setting that is unset, for a command that needs them all."""
        missing = [f"{ENV_PREFIX}{name.upper()}" for name in names if getattr(self, name) is None]
        if missing:
            raise ConfigError(f"required for this command but not set: {', '.join(missing)}")

    def secret(self, name: str) -> str:
        """The plain value of a required secret setting; ConfigError when it is unset."""
        self.require(name)
        value = getattr(self, name)
        return value.get_secret_value() if isinstance(value, SecretStr) else str(value)

    def secret_values(self) -> list[str]:
        """Plain values of every secret setting and of compose-only secrets present in the environment, for log redaction."""
        secrets = [self.db_password.get_secret_value()]
        for secret in (self.discord_token, self.discord_client_secret, self.vc_client_secret, self.secret_key):
            if secret is not None:
                secrets.append(secret.get_secret_value())
        secrets.extend(value for name in FOREIGN_SECRET_ENV_VARS if (value := os.environ.get(name)))
        return list(dict.fromkeys(secrets))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and validate settings once per process; raises pydantic.ValidationError or ConfigError on invalid input."""
    return Settings()


def reset_settings_cache() -> None:
    """Forget the cached Settings so the next get_settings() reloads the environment."""
    get_settings.cache_clear()


def format_validation_error(exc: ValidationError) -> str:
    """Describe failing settings by environment variable name and reason, never including the offending values."""
    lines = ["Invalid configuration:"]
    for error in exc.errors(include_input=False, include_url=False):
        loc = error["loc"]
        name = f"{ENV_PREFIX}{str(loc[0]).upper()}" if loc else "(settings)"
        lines.append(f"  {name}: {error['msg']}")
    return "\n".join(lines)
