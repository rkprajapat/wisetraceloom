import logging
import time
from logging import FileHandler
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

from trailwise.config import RotationConfig
from trailwise.rotation import SizeAndTimeRotatingFileHandler, build_rotating_handler


def _logger_with_handler(handler: logging.Handler) -> logging.Logger:
    logger = logging.getLogger(f"test-rotation-{id(handler)}")
    logger.setLevel(logging.INFO)
    logger.handlers = [handler]
    logger.propagate = False
    return logger


def test_size_only_config_selects_plain_rotating_file_handler(tmp_path):
    handler = build_rotating_handler(str(tmp_path / "a.log"), RotationConfig(max_size_mb=10))
    try:
        assert type(handler) is RotatingFileHandler
    finally:
        handler.close()


def test_time_only_config_selects_plain_timed_rotating_file_handler(tmp_path):
    handler = build_rotating_handler(str(tmp_path / "a.log"), RotationConfig(rotation_interval="midnight"))
    try:
        assert type(handler) is TimedRotatingFileHandler
    finally:
        handler.close()


def test_size_and_time_config_selects_combined_handler(tmp_path):
    config = RotationConfig(max_size_mb=10, rotation_interval="midnight")
    handler = build_rotating_handler(str(tmp_path / "a.log"), config)
    try:
        assert isinstance(handler, SizeAndTimeRotatingFileHandler)
    finally:
        handler.close()


def test_no_trigger_config_selects_plain_file_handler(tmp_path):
    handler = build_rotating_handler(str(tmp_path / "a.log"), RotationConfig())
    try:
        assert type(handler) is FileHandler
    finally:
        handler.close()


def test_size_trigger_rotates_the_log_file(tmp_path):
    log_file = tmp_path / "app.log"
    # ~200 bytes so a handful of log lines exceed it.
    config = RotationConfig(max_size_mb=200 / (1024 * 1024), backup_count=3)
    handler = build_rotating_handler(str(log_file), config)
    logger = _logger_with_handler(handler)

    for i in range(50):
        logger.info("x" * 20 + f" {i}")
    handler.close()

    assert list(tmp_path.glob("app.log.*"))


def test_combined_handler_rotates_on_time_trigger_even_under_size_threshold(tmp_path):
    log_file = tmp_path / "app.log"
    config = RotationConfig(max_size_mb=1000, rotation_interval="S", backup_count=3)
    handler = build_rotating_handler(str(log_file), config)
    logger = _logger_with_handler(handler)

    logger.info("first entry")
    # Force the time trigger deterministically instead of sleeping a real
    # interval — must stay a valid recent timestamp (not 0) since doRollover
    # derives a time tuple from it via time.localtime().
    handler.rolloverAt = int(time.time()) - 1
    logger.info("second entry forces rollover")
    handler.close()

    assert list(tmp_path.glob("app.log.*"))


def test_compress_backups_gzips_the_rotated_file(tmp_path):
    log_file = tmp_path / "app.log"
    config = RotationConfig(max_size_mb=200 / (1024 * 1024), backup_count=3, compress_backups=True)
    handler = build_rotating_handler(str(log_file), config)
    logger = _logger_with_handler(handler)

    for i in range(50):
        logger.info("x" * 20 + f" {i}")
    handler.close()

    assert list(tmp_path.glob("app.log.*.gz"))
    # The gzip rotator removes the uncompressed intermediate backup.
    assert not list(tmp_path.glob("app.log.[0-9]"))
