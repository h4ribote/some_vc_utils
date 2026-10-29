import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import backup
from conftest import query


def make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
    conn.commit()
    conn.close()


def test_backup_is_consistent_copy(prod_db_copy, tmp_path):
    target = backup.backup_database(prod_db_copy, tmp_path / "backups")
    assert backup.BACKUP_NAME_RE.match(target.name)
    src, dst = sqlite3.connect(prod_db_copy), sqlite3.connect(target)
    assert list(src.iterdump()) == list(dst.iterdump())
    assert dst.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_backup_while_writer_holds_transaction(tmp_path):
    db = tmp_path / "db.sqlite"
    make_db(db)
    writer = sqlite3.connect(db)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO t VALUES (999)")
    target = backup.backup_database(db, tmp_path / "backups")
    writer.rollback()
    writer.close()
    assert query(target, "SELECT count(*) FROM t") == [(100,)]


def test_missing_database_is_an_error_and_not_created(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup.backup_database(tmp_path / "absent.db", tmp_path / "backups")
    assert not (tmp_path / "absent.db").exists()


def test_retention_prunes_oldest(tmp_path):
    db = tmp_path / "db.sqlite"
    make_db(db)
    bdir = tmp_path / "backups"
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    made = [backup.backup_database(db, bdir, now=base + timedelta(hours=6 * i)) for i in range(5)]
    (bdir / "notes.txt").write_text("keep me")
    (bdir / ".database-20260101T000000.000000Z.db.partial").write_text("partial")
    deleted = backup.prune_backups(bdir, 3)
    assert deleted == made[:2]
    assert backup.list_backups(bdir) == made[2:]
    assert (bdir / "notes.txt").exists()
    assert backup.prune_backups(bdir, 3) == []
    with pytest.raises(ValueError):
        backup.prune_backups(bdir, 0)


def test_next_backup_follows_newest(tmp_path):
    db = tmp_path / "db.sqlite"
    make_db(db)
    bdir = tmp_path / "backups"
    assert backup.seconds_until_next_backup(bdir, 3600) == 0.0
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    backup.backup_database(db, bdir, now=when)
    assert backup.seconds_until_next_backup(bdir, 3600, now=when.timestamp() + 600) == pytest.approx(3000)
    assert backup.seconds_until_next_backup(bdir, 3600, now=when.timestamp() + 7200) == 0.0


def test_backup_loop_survives_failure(tmp_path, monkeypatch, caplog):
    db = tmp_path / "db.sqlite"
    make_db(db)
    bdir = tmp_path / "backups"
    real = backup.run_backup
    calls = []

    def flaky(*args):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk full")
        return real(*args)

    monkeypatch.setattr(backup, "run_backup", flaky)

    async def scenario():
        task = asyncio.create_task(backup.backup_loop(0.1 / 3600, 2, db, bdir))
        await asyncio.sleep(0.55)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.INFO, logger="backup"):
        asyncio.run(scenario())
    assert "Database backup failed" in caplog.text and "disk full" in caplog.text
    assert len(calls) >= 3
    assert len(backup.list_backups(bdir)) == 2
