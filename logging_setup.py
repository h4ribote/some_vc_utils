"""Logging to stdout and a size-rotated file under <repo>/logs, shared by the bot and discord.py."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable, TextIO

from settings import LOG_DIR, Settings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_FILE_NAME = "bot.log"
REDACTED = "[REDACTED]"


class RedactSecrets(logging.Filter):
    """Replaces configured secret strings in the message and traceback of every record that passes the handler."""

    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = self._redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self._redact(record.exc_text)
        return True


def setup_logging(settings: Settings, log_dir: Path = LOG_DIR, stream: TextIO = sys.stdout) -> Path:
    """Configure the root logger (which discord.py propagates to) and return the log file path."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / LOG_FILE_NAME
    formatter = logging.Formatter(LOG_FORMAT)
    redact = RedactSecrets(settings.secrets())

    file_handler = RotatingFileHandler(log_file, maxBytes=settings.log_max_bytes, backupCount=settings.log_backup_count, encoding="utf-8")
    stream_handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in (stream_handler, file_handler):
        handler.setFormatter(formatter)
        handler.addFilter(redact)
        root.addHandler(handler)
    root.setLevel(settings.log_level)

    discord_logger = logging.getLogger("discord")
    for handler in list(discord_logger.handlers):
        discord_logger.removeHandler(handler)
    discord_logger.propagate = True
    discord_logger.setLevel(logging.INFO if settings.log_level == "DEBUG" else settings.log_level)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    return log_file
