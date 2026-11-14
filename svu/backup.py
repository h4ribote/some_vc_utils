"""PostgreSQL backups with pg_dump's custom format into the backups volume, count-based retention, and restore with pg_restore."""

from __future__ import annotations

import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from svu.config import Settings

BACKUP_NAME_RE = re.compile(r"^svu-(\d{8}T\d{6}Z)\.dump$")
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"


class BackupError(Exception):
    """pg_dump or pg_restore failed; the message carries the tail of its standard error."""


def list_backups(backup_dir: Path) -> list[Path]:
    """Completed backup files, oldest first; temporary and foreign files are ignored."""
    if not backup_dir.is_dir():
        return []
    return sorted((p for p in backup_dir.iterdir() if p.is_file() and BACKUP_NAME_RE.match(p.name)), key=lambda p: p.name)


def backup_time(path: Path) -> datetime:
    match = BACKUP_NAME_RE.match(path.name)
    if not match:
        raise ValueError(f"not a backup file name: {path.name}")
    return datetime.strptime(match.group(1), TIMESTAMP_FORMAT).replace(tzinfo=UTC)


def is_due(backup_dir: Path, interval_seconds: int, now: datetime) -> bool:
    """A backup is due one interval after the newest existing backup, or at once when there is none."""
    backups = list_backups(backup_dir)
    return not backups or (now - backup_time(backups[-1])).total_seconds() >= interval_seconds


def _pg_env(settings: Settings) -> dict[str, str]:
    return {
        **os.environ,
        "PGHOST": settings.db_host,
        "PGPORT": str(settings.db_port),
        "PGUSER": settings.db_user,
        "PGDATABASE": settings.db_name,
        "PGPASSWORD": settings.db_password.get_secret_value(),
    }


def _run(args: list[str], settings: Settings) -> None:
    result = subprocess.run(args, env=_pg_env(settings), capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-5:])
        raise BackupError(f"{args[0]} exited with {result.returncode}: {tail}")


def take_backup(settings: Settings, now: datetime | None = None) -> Path:
    """Write a consistent snapshot of the database and return its path; a partial file never carries a backup name."""
    backup_dir = settings.backup_dir
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"svu-{(now or datetime.now(UTC)).strftime(TIMESTAMP_FORMAT)}.dump"
    partial = backup_dir / f".{target.name}.partial"
    partial.unlink(missing_ok=True)
    try:
        _run(["pg_dump", "--format=custom", "--no-owner", "--no-privileges", f"--file={partial}"], settings)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(target)
    return target


def prune_backups(backup_dir: Path, retention: int) -> list[Path]:
    """Delete the oldest backups beyond the newest retention ones and return the deleted paths."""
    backups = list_backups(backup_dir)
    doomed = backups[:-retention] if len(backups) > retention else []
    for path in doomed:
        path.unlink(missing_ok=True)
    return doomed


def restore_backup(settings: Settings, path: Path) -> None:
    """Replace the database contents with the backup at path; the other roles must be stopped first."""
    if not path.is_file():
        raise BackupError(f"backup not found: {path}")
    _run(["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges", "--single-transaction", f"--dbname={settings.db_name}", str(path)], settings)
