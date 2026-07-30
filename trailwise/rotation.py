"""Log file rotation handlers driven by a `trailwise.config.RotationConfig`.

stdlib only ships separate size-based (`RotatingFileHandler`) and time-based
(`TimedRotatingFileHandler`) implementations; `SizeAndTimeRotatingFileHandler`
merges both `shouldRollover` checks so rotation fires on whichever trigger
hits first, matching the config's combinable size/time semantics.
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
from logging import FileHandler, LogRecord
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

from trailwise.config import RotationConfig


class SizeAndTimeRotatingFileHandler(TimedRotatingFileHandler):
    """Rotates on a time interval (inherited) or a size threshold, whichever comes first."""

    def __init__(self, filename: str, *, max_bytes: int, when: str, backup_count: int, encoding: str = "utf-8") -> None:
        super().__init__(filename, when=when, backupCount=backup_count, encoding=encoding)
        self.max_bytes = max_bytes

    def shouldRollover(self, record: LogRecord) -> int:
        if super().shouldRollover(record):
            return 1
        if self.stream is None:
            self.stream = self._open()
        msg = f"{self.format(record)}\n"
        if self.stream.tell() + len(msg.encode(self.encoding or "utf-8")) >= self.max_bytes:
            return 1
        return 0


def _gzip_rotator(source: str, dest: str) -> None:
    with open(source, "rb") as f_in, gzip.open(f"{dest}.gz", "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(source)


def build_rotating_handler(filename: str, config: RotationConfig) -> logging.Handler:
    """Return the stdlib handler matching `config`'s rotation triggers."""
    has_size = config.max_size_mb is not None
    has_time = config.rotation_interval is not None

    handler: logging.Handler
    if has_size and has_time:
        handler = SizeAndTimeRotatingFileHandler(
            filename,
            max_bytes=int(config.max_size_mb * 1024 * 1024),
            when=config.rotation_interval,
            backup_count=config.backup_count,
        )
    elif has_size:
        handler = RotatingFileHandler(
            filename,
            maxBytes=int(config.max_size_mb * 1024 * 1024),
            backupCount=config.backup_count,
            encoding="utf-8",
        )
    elif has_time:
        handler = TimedRotatingFileHandler(
            filename,
            when=config.rotation_interval,
            backupCount=config.backup_count,
            encoding="utf-8",
        )
    else:
        handler = FileHandler(filename, encoding="utf-8")

    if config.compress_backups and isinstance(handler, (RotatingFileHandler, TimedRotatingFileHandler)):
        handler.rotator = _gzip_rotator

    return handler
