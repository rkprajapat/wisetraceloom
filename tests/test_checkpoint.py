from pathlib import Path

import pytest
from sqlmodel import Session, select

import wisetraceloom.checkpoint as checkpoint
import wisetraceloom.config as config
from wisetraceloom.checkpoint import should_checkpoint
from wisetraceloom.config import get_engine
from wisetraceloom.storage import StorageCheckpoint, append_commit, set_storage_config, wait_for_pending_checkpoints


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_db_path_override", str(tmp_path / "test.db"))


def test_should_checkpoint_pure_function():
    assert should_checkpoint(latest_version=10, last_checkpoint_version=0, interval=10) is True
    assert should_checkpoint(latest_version=9, last_checkpoint_version=0, interval=10) is False
    assert should_checkpoint(latest_version=10, last_checkpoint_version=10, interval=10) is False
    assert should_checkpoint(latest_version=20, last_checkpoint_version=10, interval=10) is True
    assert should_checkpoint(latest_version=0, last_checkpoint_version=0, interval=10) is False


def test_checkpoint_fires_at_version_ten():
    for i in range(10):
        append_commit("stream-a", "event", {"n": i})
    wait_for_pending_checkpoints()

    with Session(get_engine()) as session:
        checkpoints = session.exec(select(StorageCheckpoint).where(StorageCheckpoint.stream_id == "stream-a")).all()

    assert len(checkpoints) == 1
    assert checkpoints[0].version == 10
    assert checkpoints[0].row_count == 10
    assert Path(checkpoints[0].file_path).exists()


def test_checkpoint_fires_again_at_version_twenty():
    for i in range(20):
        append_commit("stream-a", "event", {"n": i})
    wait_for_pending_checkpoints()

    with Session(get_engine()) as session:
        checkpoints = session.exec(
            select(StorageCheckpoint).where(StorageCheckpoint.stream_id == "stream-a").order_by(StorageCheckpoint.version)
        ).all()

    assert [c.version for c in checkpoints] == [10, 20]
    # The version-20 checkpoint is a full snapshot of all 20 commits, not a 10-20 delta.
    assert checkpoints[1].row_count == 20


def test_checkpoint_does_not_fire_between_intervals():
    for i in range(9):
        append_commit("stream-a", "event", {"n": i})
    wait_for_pending_checkpoints()

    with Session(get_engine()) as session:
        checkpoints = session.exec(select(StorageCheckpoint).where(StorageCheckpoint.stream_id == "stream-a")).all()

    assert checkpoints == []


def test_checkpoint_interval_is_configurable_via_storage_config():
    set_storage_config(checkpoint_interval_commits=3)
    for i in range(3):
        append_commit("stream-a", "event", {"n": i})
    wait_for_pending_checkpoints()

    with Session(get_engine()) as session:
        checkpoints = session.exec(select(StorageCheckpoint).where(StorageCheckpoint.stream_id == "stream-a")).all()

    assert len(checkpoints) == 1
    assert checkpoints[0].version == 3


def test_checkpoint_failure_does_not_fail_the_append(monkeypatch):
    def broken_write_table(table, path):
        raise RuntimeError("disk full")

    monkeypatch.setattr(checkpoint.pq, "write_table", broken_write_table)

    commits = [append_commit("stream-a", "event", {"n": i}) for i in range(10)]
    wait_for_pending_checkpoints()

    # The 10th commit is still durably written even though checkpointing failed
    # (checkpointing runs on a background thread, independent of the append).
    assert commits[-1].version == 10
    with Session(get_engine()) as session:
        checkpoints = session.exec(select(StorageCheckpoint).where(StorageCheckpoint.stream_id == "stream-a")).all()
    assert checkpoints == []
