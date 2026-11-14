"""Shared fixtures; the PostgreSQL fixtures start a throwaway docker container only when an integration test requests them, unless SVU_TEST_DB_URL names a disposable database."""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import URL, make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine

from svu.config import ENV_PREFIX, FOREIGN_SECRET_ENV_VARS, TEST_ENV_PREFIX, reset_settings_cache
from svu.db.engine import create_engine
from svu.db.migrate import upgrade
from svu.db.tables import metadata
from svu.logging import clear_secrets

INTEGRATION_DIR = Path(__file__).parent / "integration"
DB_FIXTURES = frozenset({"postgres_url", "migrated_url", "engine", "db"})
POSTGRES_IMAGE = "postgres:17"
POSTGRES_DB = "svu_test"
CONTAINER_LABEL = "svu-test=1"
START_TIMEOUT_SECONDS = 120.0

_started: set[str] = set()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if INTEGRATION_DIR in item.path.parents:
            item.add_marker(pytest.mark.integration)
            continue
        used = DB_FIXTURES.intersection(getattr(item, "fixturenames", ()))
        if used:
            raise pytest.UsageError(f"{item.nodeid} uses database fixtures {sorted(used)} but is not under tests/integration")


def pytest_unconfigure(config: pytest.Config) -> None:
    for name in list(_started):
        _docker("rm", "--force", "--volumes", name, check=False)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Drop SVU_* variables, run from an empty directory so no .env is read, and restore global logging state afterwards."""
    for key in list(os.environ):
        if (key.startswith(ENV_PREFIX) and not key.startswith(TEST_ENV_PREFIX)) or key in FOREIGN_SECRET_ENV_VARS:
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    reset_settings_cache()
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    logging.captureWarnings(False)
    clear_secrets()
    reset_settings_cache()


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["docker", *args], check=False, capture_output=True, text=True, timeout=300)
    if check and result.returncode != 0:
        pytest.fail(f"docker {args[0]} failed: {result.stderr.strip()}")
    return result


async def _select_one(url: URL) -> None:
    conn = await asyncpg.connect(host=url.host, port=url.port, user=url.username, password=url.password, database=url.database, timeout=5)
    try:
        await conn.fetchval("SELECT 1")
    finally:
        await conn.close()


def _wait_ready(name: str, url: URL) -> None:
    import asyncio

    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    last: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            asyncio.run(_select_one(url))
            return
        except Exception as exc:
            last = exc
            time.sleep(0.5)
    logs = _docker("logs", "--tail", "40", name, check=False)
    pytest.fail(f"PostgreSQL container {name} was not ready ({last!r}).\n{logs.stdout}{logs.stderr}")


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[URL]:
    """URL of an empty disposable database: SVU_TEST_DB_URL (its name must end in "_test") or a throwaway container."""
    external = os.environ.get("SVU_TEST_DB_URL")
    if external:
        url = make_url(external)
        if not (url.database or "").endswith("_test"):
            raise pytest.UsageError("SVU_TEST_DB_URL must name a database ending in '_test'")
        yield url
        return
    if shutil.which("docker") is None:
        pytest.fail("Integration tests need SVU_TEST_DB_URL or a docker CLI to start PostgreSQL.")
    name = f"svu-test-pg-{uuid.uuid4().hex[:12]}"
    password = secrets.token_hex(16)
    _started.add(name)
    try:
        _docker(
            "run", "--detach", "--name", name, "--label", CONTAINER_LABEL, "--publish", "127.0.0.1::5432", "--tmpfs", "/var/lib/postgresql/data:rw",
            "--env", f"POSTGRES_PASSWORD={password}", "--env", "POSTGRES_USER=svu", "--env", f"POSTGRES_DB={POSTGRES_DB}",
            POSTGRES_IMAGE, "-c", "fsync=off", "-c", "timezone=Asia/Tokyo",
        )  # fmt: skip
        port = int(_docker("port", name, "5432/tcp").stdout.splitlines()[0].rsplit(":", 1)[1])
        url = URL.create("postgresql+asyncpg", username="svu", password=password, host="127.0.0.1", port=port, database=POSTGRES_DB)
        _wait_ready(name, url)
        yield url
    finally:
        _docker("rm", "--force", "--volumes", name, check=False)
        _started.discard(name)


@pytest.fixture(scope="session")
def migrated_url(postgres_url: URL) -> URL:
    upgrade(postgres_url, "head")
    return postgres_url


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def engine(migrated_url: URL) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(migrated_url)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def db(engine: AsyncEngine) -> AsyncEngine:
    """The session engine after emptying every table."""
    async with engine.begin() as conn:
        names = ", ".join(t.name for t in metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    return engine
