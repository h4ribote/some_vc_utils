import asyncio
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import db_utils  # noqa: E402

# A copy of the production database used read-only as a fixture source; tests that need it are skipped when it is absent.
PROD_DB_ENV = "SVU_TEST_PROD_DB"
PROD_DB = Path(os.environ.get(PROD_DB_ENV, "/nonexistent/database.db"))


@pytest.fixture
def prod_db_copy(tmp_path) -> Path:
    if not PROD_DB.is_file():
        pytest.skip(f"production database copy not available (set {PROD_DB_ENV})")
    target = tmp_path / "prod-copy.db"
    shutil.copyfile(PROD_DB, target)
    return target


@pytest.fixture
def db_path(tmp_path, monkeypatch) -> Path:
    """A fresh database with the bot's schema, installed as the default path of db_utils.Database."""
    path = tmp_path / "data" / "database.db"
    monkeypatch.setattr(db_utils, "DB_PATH", path)
    db_utils.init_database()
    return path


def query(path: Path, sql: str, params=()) -> list:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def member(user_id: int, bot: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, bot=bot, mention=f"<@{user_id}>", name=f"user{user_id}")


class FakeVC:
    """Stand-in for AsyncVirtualCryptoClient that records payments and can delay or fail them."""

    def __init__(self, delay: float = 0.0, fail_ids=(), on_pay=None):
        self.delay = delay
        self.fail_ids = set(fail_ids)
        self.on_pay = on_pay
        self.payments = []
        self.attempts = []
        self.started = False
        self.closed = False

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    async def pay(self, unit, user_id, amount):
        self.attempts.append((unit, user_id, amount))
        if self.on_pay:
            self.on_pay(unit, user_id, amount)
        if self.delay:
            await asyncio.sleep(self.delay)
        if user_id in self.fail_ids:
            raise RuntimeError(f"payment to {user_id} rejected")
        self.payments.append((unit, user_id, amount))

    async def create_claim(self, payer_id, unit, amount):
        return SimpleNamespace(id=4242, payer=payer_id, unit=unit, amount=amount)
