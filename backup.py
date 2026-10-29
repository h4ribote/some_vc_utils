"""Consistent SQLite backups into <repo>/backups with count-based retention, as a bot background task and as a CLI (python backup.py)."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from settings import BACKUP_DIR, DB_PATH, ConfigError, load_settings

log = logging.getLogger(__name__)

BACKUP_NAME_RE = re.compile(r"^database-(\d{8}T\d{6}\.\d{6}Z)\.db$")
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S.%fZ"


def list_backups(backup_dir: Path = BACKUP_DIR) -> list[Path]:
    """Return completed backup files, oldest first; temporary and foreign files are ignored."""
    if not backup_dir.is_dir():
        return []
    return sorted((p for p in backup_dir.iterdir() if p.is_file() and BACKUP_NAME_RE.match(p.name)), key=lambda p: p.name)


def backup_time(path: Path) -> datetime:
    match = BACKUP_NAME_RE.match(path.name)
    if not match:
        raise ValueError(f"not a backup file name: {path.name}")
    return datetime.strptime(match.group(1), TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


def backup_database(db_path: Path = DB_PATH, backup_dir: Path = BACKUP_DIR, now: Optional[datetime] = None) -> Path:
    """Write a consistent snapshot of db_path with the sqlite3 backup API and return its path."""
    if not db_path.is_file():
        raise FileNotFoundError(f"database not found: {db_path}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)
    target = backup_dir / f"database-{stamp}.db"
    partial = backup_dir / f".{target.name}.partial"
    partial.unlink(missing_ok=True)
    # The source is opened read-only so a wrong path never creates an empty database.
    src = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(partial)
        try:
            src.backup(dst)
        finally:
            dst.close()
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        src.close()
    partial.replace(target)
    return target


def prune_backups(backup_dir: Path, retention: int) -> list[Path]:
    """Delete the oldest backups beyond the newest `retention` ones and return the deleted paths."""
    if retention < 1:
        raise ValueError("retention must be at least 1")
    backups = list_backups(backup_dir)
    doomed = backups[:-retention] if len(backups) > retention else []
    for path in doomed:
        path.unlink(missing_ok=True)
    return doomed


def run_backup(db_path: Path, backup_dir: Path, retention: int) -> tuple[Path, list[Path]]:
    target = backup_database(db_path, backup_dir)
    return target, prune_backups(backup_dir, retention)


def seconds_until_next_backup(backup_dir: Path, interval_seconds: float, now: Optional[float] = None) -> float:
    """The next backup is due one interval after the newest existing backup, or immediately when there is none."""
    backups = list_backups(backup_dir)
    if not backups:
        return 0.0
    now = time.time() if now is None else now
    return max(0.0, backup_time(backups[-1]).timestamp() + interval_seconds - now)


async def backup_loop(interval_hours: float, retention: int, db_path: Path = DB_PATH, backup_dir: Path = BACKUP_DIR) -> None:
    """Back up the database every interval until cancelled; a failed backup is logged and retried one interval later."""
    interval = interval_hours * 3600
    delay = seconds_until_next_backup(backup_dir, interval)
    log.info("Database backups every %s h, keeping %d; next in %d s", interval_hours, retention, int(delay))
    while True:
        await asyncio.sleep(delay)
        try:
            target, pruned = await asyncio.to_thread(run_backup, db_path, backup_dir, retention)
            log.info("Database backup written to %s (%d old backup(s) pruned)", target.name, len(pruned))
        except Exception:
            log.exception("Database backup failed")
        delay = interval


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    try:
        settings = load_settings(require_secrets=False)
    except ConfigError as e:
        print(e.report(), file=sys.stderr)
        return 2
    try:
        target, pruned = run_backup(DB_PATH, BACKUP_DIR, settings.backup_retention)
    except Exception as e:
        log.error("Backup failed: %s: %s", type(e).__name__, e)
        return 1
    log.info("Backup written to %s", target)
    for path in pruned:
        log.info("Pruned old backup %s", path.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
