import pytest
from sqlmodel import Session, select

import wisetraceloom.config as config
from wisetraceloom.audit_chain import (
    anchor_commits,
    compute_merkle_root,
    verify_anchor,
    verify_chain,
)
from wisetraceloom.config import get_engine
from wisetraceloom.storage import StorageCommit, append_commit


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_db_path_override", str(tmp_path / "test.db"))


def _mutate_commit(stream_id: str, version: int, **changes):
    with Session(get_engine()) as session:
        row = session.exec(
            select(StorageCommit).where(StorageCommit.stream_id == stream_id, StorageCommit.version == version)
        ).one()
        for field, value in changes.items():
            setattr(row, field, value)
        session.add(row)
        session.commit()


def test_first_commit_has_no_prev_hash():
    commit = append_commit("stream-a", "event", {"n": 0})
    assert commit.prev_hash is None
    assert commit.entry_hash


def test_second_commit_chains_onto_first():
    first = append_commit("stream-a", "event", {"n": 0})
    second = append_commit("stream-a", "event", {"n": 1})
    assert second.prev_hash == first.entry_hash
    assert second.entry_hash != first.entry_hash


def test_verify_chain_ok_on_empty_stream():
    result = verify_chain("nonexistent-stream")
    assert result.ok is True
    assert result.broken_at_version is None


def test_verify_chain_ok_on_untampered_stream():
    for i in range(5):
        append_commit("stream-a", "event", {"n": i})
    result = verify_chain("stream-a")
    assert result.ok is True


def test_verify_chain_detects_tampered_payload():
    for i in range(3):
        append_commit("stream-a", "event", {"n": i})
    _mutate_commit("stream-a", 2, payload='{"n": 999}')

    result = verify_chain("stream-a")

    assert result.ok is False
    assert result.broken_at_version == 2


def test_verify_chain_detects_broken_link():
    for i in range(3):
        append_commit("stream-a", "event", {"n": i})
    _mutate_commit("stream-a", 2, prev_hash="not-the-real-prior-hash")

    result = verify_chain("stream-a")

    assert result.ok is False
    assert result.broken_at_version == 2


def test_verify_chain_ok_after_concurrent_writers():
    from concurrent.futures import ThreadPoolExecutor

    n = 20
    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(lambda i: append_commit("stream-a", "event", {"n": i}), range(n)))

    result = verify_chain("stream-a")
    assert result.ok is True


def test_verify_chain_is_isolated_per_stream():
    append_commit("stream-a", "event", {"n": 0})
    append_commit("stream-b", "event", {"n": 0})
    _mutate_commit("stream-a", 1, payload='{"n": 999}')

    assert verify_chain("stream-a").ok is False
    assert verify_chain("stream-b").ok is True


def test_compute_merkle_root_single_leaf_is_itself():
    assert compute_merkle_root(["abc"]) == "abc"


def test_compute_merkle_root_empty_raises():
    with pytest.raises(ValueError):
        compute_merkle_root([])


def test_compute_merkle_root_order_sensitive():
    root_1 = compute_merkle_root(["a", "b", "c"])
    root_2 = compute_merkle_root(["c", "b", "a"])
    assert root_1 != root_2


def test_compute_merkle_root_deterministic():
    leaves = ["a", "b", "c", "d", "e"]
    assert compute_merkle_root(leaves) == compute_merkle_root(list(leaves))


def test_anchor_commits_calls_sink_with_stream_and_root_and_persists_reference():
    for i in range(3):
        append_commit("stream-a", "event", {"n": i})

    calls = []

    def fake_sink(stream_id, root):
        calls.append((stream_id, root))
        return "external-ref-123"

    record = anchor_commits("stream-a", fake_sink)

    assert calls == [("stream-a", record.merkle_root)]
    assert record.external_reference == "external-ref-123"
    assert record.version == 3


def test_anchor_commits_raises_for_stream_with_no_commits():
    with pytest.raises(ValueError):
        anchor_commits("empty-stream", lambda stream_id, root: "ref")


def test_verify_anchor_true_for_untampered_stream():
    for i in range(4):
        append_commit("stream-a", "event", {"n": i})
    record = anchor_commits("stream-a", lambda stream_id, root: "ref")

    assert verify_anchor(record) is True


def test_verify_anchor_false_after_entry_hash_forged():
    # verify_anchor recomputes the Merkle root from each commit's currently
    # stored entry_hash — it catches an attacker who edited entry_hash
    # itself (e.g. to relink a locally-forged chain), which is precisely
    # what an external anchor is for: verify_chain alone can be fooled by a
    # self-consistent local rewrite, but that rewrite can't reproduce a
    # Merkle root that was already handed to an external sink beforehand.
    for i in range(4):
        append_commit("stream-a", "event", {"n": i})
    record = anchor_commits("stream-a", lambda stream_id, root: "ref")

    _mutate_commit("stream-a", 2, entry_hash="forged" * 8)

    assert verify_anchor(record) is False


def test_verify_anchor_false_if_stream_shorter_than_anchored_version():
    for i in range(4):
        append_commit("stream-a", "event", {"n": i})
    record = anchor_commits("stream-a", lambda stream_id, root: "ref")

    with Session(get_engine()) as session:
        row = session.exec(
            select(StorageCommit).where(StorageCommit.stream_id == "stream-a", StorageCommit.version == 4)
        ).one()
        session.delete(row)
        session.commit()

    assert verify_anchor(record) is False
