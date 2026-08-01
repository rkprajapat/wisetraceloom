from pathlib import Path

import pytest
from sqlmodel import Session, select

import wisetraceloom.config as config
import wisetraceloom.residency as residency
from wisetraceloom.config import get_engine
from wisetraceloom.residency import (
    UnroutedRegionError,
    get_region_db_path,
    register_region,
    resolve_engine,
    resolve_region,
    set_region_config,
)
from wisetraceloom.storage import StorageCommit, append_commit, read_latest, set_storage_config, wait_for_pending_checkpoints


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_db_path_override", str(tmp_path / "test.db"))
    # register_region writes to a process-global registry by design (a host
    # registers its regions once at startup, mirroring set_db_path) — reset
    # it per test the same way _db_path_override is reset, so one test's
    # region labels don't leak into another's.
    monkeypatch.setattr(residency, "_region_db_paths", {})
    return tmp_path


def test_resolve_region_none_when_unconfigured():
    assert resolve_region("acme") is None


def test_resolve_engine_falls_back_to_default_when_unconfigured():
    assert resolve_engine("acme") is get_engine()
    assert resolve_engine(None) is get_engine()


def test_register_region_then_route_tenant(tmp_path):
    region_path = str(tmp_path / "ap-south-1.db")
    register_region("ap-south-1", region_path)
    set_region_config(tenant_id="acme", region="ap-south-1")

    assert resolve_region("acme") == "ap-south-1"
    engine = resolve_engine("acme")
    assert engine is not get_engine()
    assert get_region_db_path("ap-south-1") == region_path


def test_unregistered_region_raises(tmp_path):
    set_region_config(tenant_id="acme", region="ap-south-1")  # never registered
    with pytest.raises(UnroutedRegionError):
        resolve_engine("acme")


def test_tenant_specific_region_overrides_global_default(tmp_path):
    register_region("eu-west-1", str(tmp_path / "eu.db"))
    register_region("ap-south-1", str(tmp_path / "in.db"))
    set_region_config(region="eu-west-1")  # global default
    set_region_config(tenant_id="acme", region="ap-south-1")

    assert resolve_region("acme") == "ap-south-1"
    assert resolve_region("other-tenant") == "eu-west-1"


def test_unconfigured_tenant_untouched_by_unrelated_region_config(tmp_path):
    register_region("ap-south-1", str(tmp_path / "in.db"))
    set_region_config(tenant_id="acme", region="ap-south-1")

    assert resolve_region("other-tenant") is None
    assert resolve_engine("other-tenant") is get_engine()


def test_append_commit_routes_regulated_tenant_to_region_file(tmp_path):
    region_path = str(tmp_path / "ap-south-1.db")
    register_region("ap-south-1", region_path)
    set_region_config(tenant_id="acme", region="ap-south-1")

    append_commit("spans", "event", {"n": 1}, tenant_id="acme")
    append_commit("spans", "event", {"n": 2}, tenant_id="other-tenant")

    from wisetraceloom.config import get_engine_for_path

    with Session(get_engine_for_path(region_path)) as session:
        region_rows = session.exec(select(StorageCommit).where(StorageCommit.stream_id == "spans")).all()
    with Session(get_engine()) as session:
        default_rows = session.exec(select(StorageCommit).where(StorageCommit.stream_id == "spans")).all()

    assert [row.tenant_id for row in region_rows] == ["acme"]
    assert [row.tenant_id for row in default_rows] == ["other-tenant"]


def test_read_latest_for_routed_tenant_reads_from_region_file(tmp_path):
    register_region("ap-south-1", str(tmp_path / "ap-south-1.db"))
    set_region_config(tenant_id="acme", region="ap-south-1")

    append_commit("spans", "event", {"n": 1}, tenant_id="acme")
    append_commit("spans", "event", {"n": 2}, tenant_id="acme")

    rows = read_latest("spans", tenant_id="acme")

    assert [row["n"] for row in rows] == [1, 2]


def test_read_latest_without_tenant_id_only_sees_default_store(tmp_path):
    register_region("ap-south-1", str(tmp_path / "ap-south-1.db"))
    set_region_config(tenant_id="acme", region="ap-south-1")

    append_commit("spans", "event", {"n": 1}, tenant_id="acme")
    append_commit("spans", "event", {"n": 2}, tenant_id="other-tenant")

    # No tenant_id -> only the default store is queried, so the region-routed
    # tenant's commit (physically in a different file) is invisible here.
    rows = read_latest("spans")

    assert [row["n"] for row in rows] == [2]


def test_checkpoint_fires_for_region_routed_stream_in_its_own_directory(tmp_path):
    region_path = str(tmp_path / "ap-south-1.db")
    register_region("ap-south-1", region_path)
    set_region_config(tenant_id="acme", region="ap-south-1")
    set_storage_config(checkpoint_interval_commits=3)

    for i in range(3):
        append_commit("spans", "event", {"n": i}, tenant_id="acme")
    wait_for_pending_checkpoints()

    from wisetraceloom.config import get_engine_for_path
    from wisetraceloom.storage import StorageCheckpoint

    with Session(get_engine_for_path(region_path)) as session:
        checkpoints = session.exec(select(StorageCheckpoint).where(StorageCheckpoint.stream_id == "spans")).all()

    assert len(checkpoints) == 1
    assert checkpoints[0].version == 3
    # Checkpoint file lives under the region file's own directory, not the
    # default store's — physically co-located with the data it snapshots.
    assert Path(checkpoints[0].file_path).is_relative_to(tmp_path)
    assert Path(checkpoints[0].file_path).exists()


def test_default_store_and_region_store_checkpoints_do_not_collide(tmp_path):
    region_path = str(tmp_path / "ap-south-1.db")
    register_region("ap-south-1", region_path)
    set_region_config(tenant_id="acme", region="ap-south-1")
    set_storage_config(checkpoint_interval_commits=2)

    for i in range(2):
        append_commit("spans", "event", {"n": i}, tenant_id="acme")  # -> region store
    for i in range(2):
        append_commit("spans", "event", {"n": i}, tenant_id="other-tenant")  # -> default store
    wait_for_pending_checkpoints()

    from wisetraceloom.config import get_engine_for_path
    from wisetraceloom.storage import StorageCheckpoint

    with Session(get_engine_for_path(region_path)) as session:
        region_checkpoints = session.exec(select(StorageCheckpoint).where(StorageCheckpoint.stream_id == "spans")).all()
    with Session(get_engine()) as session:
        default_checkpoints = session.exec(select(StorageCheckpoint).where(StorageCheckpoint.stream_id == "spans")).all()

    assert len(region_checkpoints) == 1
    assert len(default_checkpoints) == 1
    assert region_checkpoints[0].file_path != default_checkpoints[0].file_path
    assert Path(region_checkpoints[0].file_path).exists()
    assert Path(default_checkpoints[0].file_path).exists()
