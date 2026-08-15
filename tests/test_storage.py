from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session

import wisetraceloom.config as config
import wisetraceloom.storage as storage
from wisetraceloom.config import get_engine
from wisetraceloom.instrumentation import tool_call
from wisetraceloom.storage import (
    StorageCommit,
    StorageConflictError,
    append_commit,
    read_as_of_timestamp,
    read_as_of_version,
    read_latest,
    set_storage_config,
    wait_for_pending_checkpoints,
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    # Own SQLite file per test — the process-wide engine cache (keyed by
    # path) never leaks state between tests; checkpoint files land under
    # tmp_path for free since checkpoint_dir defaults relative to get_db_path().
    monkeypatch.setattr(config, "_db_path_override", str(tmp_path / "test.db"))


def test_append_commit_assigns_sequential_versions_starting_at_one():
    first = append_commit("stream-a", "event", {"n": 1})
    second = append_commit("stream-a", "event", {"n": 2})
    assert first.version == 1
    assert second.version == 2


def test_append_commit_is_isolated_per_stream_id():
    a1 = append_commit("stream-a", "event", {"n": 1})
    b1 = append_commit("stream-b", "event", {"n": 1})
    assert a1.version == 1
    assert b1.version == 1


def test_append_commit_retries_on_version_conflict(monkeypatch):
    # Pre-seed a row at version 1 directly, then force _next_version to
    # return the stale value 1 once before delegating to the real
    # implementation — append_commit must catch the resulting IntegrityError
    # and retry with a freshly computed version instead of raising.
    with Session(get_engine()) as session:
        session.add(StorageCommit(stream_id="stream-a", version=1, record_type="event", payload="{}"))
        session.commit()

    real_next_version = storage._next_version
    calls = {"count": 0}

    def flaky_next_version(session, stream_id):
        calls["count"] += 1
        if calls["count"] == 1:
            return 1
        return real_next_version(session, stream_id)

    monkeypatch.setattr(storage, "_next_version", flaky_next_version)

    commit = append_commit("stream-a", "event", {"n": 2})
    assert commit.version == 2
    assert calls["count"] == 2


def test_append_commit_raises_storage_conflict_error_after_max_retries_exhausted(monkeypatch):
    with Session(get_engine()) as session:
        session.add(StorageCommit(stream_id="stream-a", version=1, record_type="event", payload="{}"))
        session.commit()

    monkeypatch.setattr(storage, "_next_version", lambda session, stream_id: 1)

    with pytest.raises(StorageConflictError):
        append_commit("stream-a", "event", {"n": 2}, max_retries=3)


def test_append_commit_concurrent_writers_no_lost_updates():
    from concurrent.futures import ThreadPoolExecutor

    n = 20
    with ThreadPoolExecutor(max_workers=n) as pool:
        commits = list(pool.map(lambda i: append_commit("stream-a", "event", {"n": i}), range(n)))

    versions = sorted(c.version for c in commits)
    assert versions == list(range(1, n + 1))


def test_read_as_of_version_returns_correct_state():
    for i in range(5):
        append_commit("stream-a", "event", {"n": i})

    state = read_as_of_version("stream-a", 3)
    assert [row["n"] for row in state] == [0, 1, 2]


def test_read_as_of_version_reconciles_checkpoint_plus_commits_since():
    set_storage_config(checkpoint_interval_commits=10)
    for i in range(15):
        append_commit("stream-a", "event", {"n": i})
    # Checkpointing runs off the append_commit critical path (background
    # thread) — wait for it so this test deterministically exercises the
    # checkpoint+tail reconciliation path rather than a pure-tail replay.
    wait_for_pending_checkpoints()

    full = read_as_of_version("stream-a", 15)
    assert [row["n"] for row in full] == list(range(15))

    up_to_checkpoint = read_as_of_version("stream-a", 10)
    assert [row["n"] for row in up_to_checkpoint] == list(range(10))


def test_read_as_of_timestamp_resolves_to_correct_version():
    import time

    append_commit("stream-a", "event", {"n": 0})
    time.sleep(0.01)
    cutoff = datetime.now(timezone.utc)
    time.sleep(0.01)
    append_commit("stream-a", "event", {"n": 1})

    state = read_as_of_timestamp("stream-a", cutoff)
    assert [row["n"] for row in state] == [0]


def test_read_as_of_timestamp_before_any_commit_returns_empty():
    append_commit("stream-a", "event", {"n": 0})
    before = datetime.now(timezone.utc) - timedelta(days=1)
    assert read_as_of_timestamp("stream-a", before) == []


def test_read_latest_returns_all_commits():
    for i in range(3):
        append_commit("stream-a", "event", {"n": i})
    assert [row["n"] for row in read_latest("stream-a")] == [0, 1, 2]


def test_read_as_of_version_isolated_per_stream_id():
    append_commit("stream-a", "event", {"n": "a"})
    append_commit("stream-b", "event", {"n": "b"})
    assert [row["n"] for row in read_latest("stream-a")] == ["a"]
    assert [row["n"] for row in read_latest("stream-b")] == ["b"]


def test_storage_config_tenant_specific_config_overrides_global_default():
    set_storage_config(checkpoint_interval_commits=10)
    set_storage_config(tenant_id="acme", checkpoint_interval_commits=3)

    tenant_config = storage.get_storage_config(tenant_id="acme")
    other_tenant_config = storage.get_storage_config(tenant_id="other-tenant")

    assert tenant_config.checkpoint_interval_commits == 3
    # No row for "other-tenant" yet -> falls back to the global default row.
    assert other_tenant_config.checkpoint_interval_commits == 10


def test_storage_write_failure_is_fail_open(monkeypatch):
    import wisetraceloom.instrumentation as instrumentation

    def broken_enqueue(*args, **kwargs):
        raise RuntimeError("storage down")

    monkeypatch.setattr(instrumentation, "enqueue_append", broken_enqueue)

    with tool_call("search") as span:
        pass  # no exception should escape despite the broken storage write

    assert span.success is True


def test_tool_call_persists_span_to_storage():
    with tool_call("search", tenant_id="acme"):
        pass
    # The span is handed to a background writer (enqueue_append), not
    # durably written inline — wait for it to land before reading.
    storage.wait_for_pending_writes()

    from wisetraceloom.tenancy import isolated_stream_id

    state = read_latest(isolated_stream_id("spans", "acme"), tenant_id="acme")
    tool_events = [row for row in state if row["tool_name"] == "search"]
    assert len(tool_events) == 1
    assert tool_events[0]["tenant_id"] == "acme"
