"""Repository paths and runtime settings read from <repo>/.env, with real environment variables taking precedence."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

REPO_ROOT = Path(__file__).resolve().parent
ENV_FILE = REPO_ROOT / ".env"
DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "database.db"
LOG_DIR = REPO_ROOT / "logs"
BACKUP_DIR = REPO_ROOT / "backups"
RUN_DIR = REPO_ROOT / "run"

# Exit status for configuration errors (sysexits EX_CONFIG); the supervisor in svuctl does not restart the bot on it.
EXIT_CONFIG_ERROR = 78

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigError(Exception):
    """Raised with every configuration problem found, so the operator can fix them all at once."""

    def __init__(self, problems: list[str], env_file: Path = ENV_FILE):
        self.problems = problems
        self.env_file = env_file
        super().__init__("; ".join(problems))

    def report(self) -> str:
        lines = [f"Configuration error ({len(self.problems)} problem(s)); settings are read from {self.env_file} and the environment:"]
        lines += [f"  - {p}" for p in self.problems]
        lines.append("See deploy/env.example for every variable.")
        return "\n".join(lines)


@dataclass(frozen=True)
class Settings:
    discord_token: str = field(repr=False)
    admin_ids: frozenset[int]
    vc_client_id: str
    vc_client_secret: str = field(repr=False)
    backup_interval_hours: float = 6.0
    backup_retention: int = 28
    log_level: str = "INFO"
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 5

    def secrets(self) -> tuple[str, ...]:
        return tuple(s for s in (self.discord_token, self.vc_client_secret) if s)


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines; blank lines, # comments, an optional "export " prefix and matching surrounding quotes are handled."""
    values: dict[str, str] = {}
    problems: list[str] = []
    with open(path, encoding="utf-8") as fp:
        for lineno, raw in enumerate(fp, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, sep, value = line.partition("=")
            key = key.strip()
            if not sep or not key.isidentifier():
                # The line content is not echoed because it may contain a secret.
                problems.append(f"{path.name} line {lineno}: expected KEY=VALUE")
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    if problems:
        raise ConfigError(problems, path)
    return values


def load_environment(env_file: Path = ENV_FILE, environ: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Return the .env values overlaid with the process environment; a missing .env file counts as empty."""
    merged: dict[str, str] = {}
    if env_file.is_file():
        merged.update(parse_env_file(env_file))
    merged.update(os.environ if environ is None else environ)
    return merged


def _int(env: Mapping[str, str], name: str, default: int, minimum: int, problems: list[str]) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        problems.append(f"{name}: must be an integer, got {raw!r}")
        return default
    if value < minimum:
        problems.append(f"{name}: must be >= {minimum}, got {value}")
    return value


def _float(env: Mapping[str, str], name: str, default: float, problems: list[str]) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        problems.append(f"{name}: must be a number, got {raw!r}")
        return default
    if not value > 0:
        problems.append(f"{name}: must be greater than 0, got {raw}")
    return value


def _admin_ids(raw: str, problems: list[str]) -> frozenset[int]:
    ids = set()
    bad = []
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        if item.isdigit() and int(item) > 0:
            ids.add(int(item))
        else:
            bad.append(item)
    if bad:
        problems.append(f"SVU_ADMIN_IDS: not Discord user ids: {', '.join(repr(b) for b in bad)}")
    elif not ids:
        problems.append("SVU_ADMIN_IDS: must list at least one Discord user id (comma separated)")
    return frozenset(ids)


def load_settings(env_file: Path = ENV_FILE, environ: Optional[Mapping[str, str]] = None, require_secrets: bool = True) -> Settings:
    """Load and validate settings; raises ConfigError naming every missing or invalid variable."""
    env = load_environment(env_file, environ)
    problems: list[str] = []

    def required(name: str) -> str:
        value = env.get(name, "").strip()
        if require_secrets and not value:
            problems.append(f"{name}: missing")
        return value

    token = required("SVU_DISCORD_TOKEN")
    if token and any(c.isspace() for c in token):
        problems.append("SVU_DISCORD_TOKEN: must not contain whitespace")
    raw_admins = required("SVU_ADMIN_IDS")
    admin_ids = _admin_ids(raw_admins, problems) if raw_admins else frozenset()
    vc_client_id = required("SVU_VC_CLIENT_ID")
    vc_client_secret = required("SVU_VC_CLIENT_SECRET")

    log_level = env.get("SVU_LOG_LEVEL", "").strip().upper() or "INFO"
    if log_level not in LOG_LEVELS:
        problems.append(f"SVU_LOG_LEVEL: must be one of {', '.join(LOG_LEVELS)}, got {log_level!r}")

    settings = Settings(
        discord_token=token,
        admin_ids=admin_ids,
        vc_client_id=vc_client_id,
        vc_client_secret=vc_client_secret,
        backup_interval_hours=_float(env, "SVU_BACKUP_INTERVAL_HOURS", 6.0, problems),
        backup_retention=_int(env, "SVU_BACKUP_RETENTION", 28, 1, problems),
        log_level=log_level,
        log_max_bytes=_int(env, "SVU_LOG_MAX_BYTES", 10 * 1024 * 1024, 1024, problems),
        log_backup_count=_int(env, "SVU_LOG_BACKUP_COUNT", 5, 1, problems),
    )
    if problems:
        raise ConfigError(problems, env_file)
    return settings
