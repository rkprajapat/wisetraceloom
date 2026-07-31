import pytest

import wisetraceloom.config as config
from wisetraceloom.config import get_rotation_config, set_rotation_config


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    # Each test gets its own SQLite file so the process-wide engine cache
    # (keyed by path) never leaks state between tests.
    monkeypatch.setattr(config, "_db_path_override", str(tmp_path / "test.db"))


def test_get_rotation_config_falls_back_to_builtin_default_when_unset():
    config = get_rotation_config()
    assert config.max_size_mb is not None or config.rotation_interval is not None


def test_set_then_get_global_rotation_config_round_trips():
    set_rotation_config(
        log_file_path="logs/app.log",
        max_size_mb=25.0,
        rotation_interval="midnight",
        backup_count=3,
        compress_backups=True,
    )

    config = get_rotation_config()

    assert config.tenant_id is None
    assert config.log_file_path == "logs/app.log"
    assert config.max_size_mb == 25.0
    assert config.rotation_interval == "midnight"
    assert config.backup_count == 3
    assert config.compress_backups is True


def test_log_file_path_defaults_to_unset():
    config = get_rotation_config()
    assert config.log_file_path is None


def test_set_rotation_config_upserts_existing_row():
    set_rotation_config(max_size_mb=10.0)
    set_rotation_config(max_size_mb=99.0)

    config = get_rotation_config()

    assert config.max_size_mb == 99.0


def test_tenant_specific_config_overrides_global_default():
    set_rotation_config(max_size_mb=10.0, rotation_interval="midnight")
    set_rotation_config(tenant_id="acme", max_size_mb=500.0, rotation_interval="H")

    tenant_config = get_rotation_config(tenant_id="acme")
    other_tenant_config = get_rotation_config(tenant_id="other-tenant")

    assert tenant_config.max_size_mb == 500.0
    assert tenant_config.rotation_interval == "H"
    # No row for "other-tenant" yet -> falls back to the global default row.
    assert other_tenant_config.max_size_mb == 10.0
    assert other_tenant_config.tenant_id is None
