"""Async engine for PostgreSQL through asyncpg, with session settings that do not depend on the server configuration."""

from __future__ import annotations

from typing import Any

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine

POOL_RECYCLE_SECONDS = 3600
ISOLATION_LEVEL = "READ COMMITTED"
LOCK_TIMEOUT_MS = 5000
STATEMENT_TIMEOUT_MS = 30000


def create_engine(url: URL | str, **kw: Any) -> AsyncEngine:
    """Create an AsyncEngine whose sessions use READ COMMITTED, UTC and bounded lock and statement waits; keyword arguments override the defaults."""
    server_settings = {
        "timezone": "UTC",
        "lock_timeout": str(LOCK_TIMEOUT_MS),
        "statement_timeout": str(STATEMENT_TIMEOUT_MS),
        "application_name": "svu",
    }
    connect_args = {"server_settings": server_settings, **kw.pop("connect_args", {})}
    options: dict[str, Any] = {"pool_pre_ping": True, "pool_recycle": POOL_RECYCLE_SECONDS, "isolation_level": ISOLATION_LEVEL, "connect_args": connect_args}
    options.update(kw)
    return _create_async_engine(url, **options)
