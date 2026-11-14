"""Programmatic Alembic entry points that work from an installed package without a repo-root alembic.ini."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import URL

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def make_config(url: URL | str | None = None) -> Config:
    """Alembic Config pointing at the packaged migrations; env.py connects to attributes["url"]."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if url is not None:
        config.attributes["url"] = url
    return config


def upgrade(url: URL | str, revision: str = "head") -> None:
    """Apply migrations up to revision; must not be called while an event loop is running in this thread."""
    command.upgrade(make_config(url), revision)


def head_revision() -> str | None:
    """The newest revision in the packaged migration scripts."""
    return ScriptDirectory.from_config(make_config()).get_current_head()
