import json

import pytest

import wisetraceloom.config as config
from wisetraceloom.config import set_rotation_config
from wisetraceloom.logging import bind_context, configure, get_logger


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_db_path_override", str(tmp_path / "cfg.db"))


def _read_json_lines(path):
    return [json.loads(line) for line in path.read_text().strip().splitlines()]


def test_configure_with_file_path_writes_json_lines_to_the_file(tmp_path):
    log_file = tmp_path / "app.log"
    try:
        configure(file_path=str(log_file))
        logger = get_logger("test.file_output")

        with bind_context(tenant_id="acme"):
            logger.info("hello-file", extra="y")

        records = _read_json_lines(log_file)
        assert len(records) == 1
        assert records[0]["event"] == "hello-file"
        assert records[0]["extra"] == "y"
        assert records[0]["tenant_id"] == "acme"
    finally:
        # Reset to console mode so later tests aren't left writing to a
        # temp file that this test's tmp_path fixture will clean up.
        configure()


def test_configure_falls_back_to_configured_log_file_path_when_not_passed(tmp_path):
    log_file = tmp_path / "configured.log"
    try:
        set_rotation_config(log_file_path=str(log_file), max_size_mb=50.0)

        configure()  # no explicit file_path -> should pick up the stored config
        get_logger("test.file_output.fallback").info("via-config")

        records = _read_json_lines(log_file)
        assert records[0]["event"] == "via-config"
    finally:
        configure()


def test_explicit_file_path_overrides_configured_log_file_path(tmp_path):
    configured_path = tmp_path / "configured.log"
    explicit_path = tmp_path / "explicit.log"
    try:
        set_rotation_config(log_file_path=str(configured_path), max_size_mb=50.0)

        configure(file_path=str(explicit_path))
        get_logger("test.file_output.override").info("via-explicit-arg")

        assert not configured_path.exists()
        records = _read_json_lines(explicit_path)
        assert records[0]["event"] == "via-explicit-arg"
    finally:
        configure()
