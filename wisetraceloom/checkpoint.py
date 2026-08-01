"""Parquet checkpoint compaction for `wisetraceloom.storage`'s append-only
commit log (PRD §1, §5 — feature 2.1).

Kept separate from `storage.py` so the pyarrow dependency's surface is
isolated in one file, mirroring the existing `config.py`/`rotation.py` split
(row model + decision logic vs. I/O-heavy handler code).

A checkpoint is a **full snapshot** of every commit in a stream from version 1
through the checkpointed version — not an incremental delta — matching
Delta Lake's own checkpoint semantics. This makes checkpointing idempotent and
self-healing: a missed checkpoint at version 10 just means the checkpoint at
version 20 replays more commits, never that a gap goes unrepaired.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from wisetraceloom.config import get_engine

if TYPE_CHECKING:
    from wisetraceloom.storage import StorageCheckpoint

_STREAM_ID_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]")


def should_checkpoint(latest_version: int, last_checkpoint_version: int, interval: int) -> bool:
    """Pure decision function: does `latest_version` cross a new checkpoint boundary?"""
    if interval <= 0:
        return False
    return latest_version > last_checkpoint_version and latest_version % interval == 0


def default_checkpoint_dir(engine: Engine) -> str:
    """Checkpoints for `engine`'s store live next to that store's own
    SQLite file. For a region-routed engine (feature 2.5's
    `wisetraceloom.residency`), this keeps the Parquet checkpoint data
    physically co-located with the region's own commit log rather than
    always defaulting under the primary store's directory — the latter
    would silently defeat the point of routing that data to a specific
    region in the first place."""
    return str(Path(engine.url.database).parent / "checkpoints")


def checkpoint_file_path(stream_id: str, version: int, checkpoint_dir: str, engine: Engine) -> Path:
    # Namespaced by the engine's own db path (not just stream_id/version):
    # two physically separate stores (default store + a region store, or
    # two different regions) can each independently reach e.g. "spans"
    # version 10 — without this, both would resolve to the identical
    # Parquet path and overwrite each other's checkpoint data, even under
    # an explicit shared `checkpoint_dir` override.
    store_label = _STREAM_ID_SANITIZE_RE.sub("_", Path(str(engine.url.database)).stem)
    safe_stream_id = _STREAM_ID_SANITIZE_RE.sub("_", stream_id)
    return Path(checkpoint_dir) / store_label / safe_stream_id / f"{version:020d}.checkpoint.parquet"


def compact_checkpoint(stream_id: str, *, engine: Engine | None = None) -> StorageCheckpoint | None:
    """Full-snapshot compaction of every commit in `stream_id` (version 1
    through the current max) into one Parquet file; upserts the
    `StorageCheckpoint` row for that `(stream_id, version)`. Returns `None`
    if the stream has no commits yet.

    `engine` defaults to the shared default store (`get_engine()`) but a
    region-routed stream (feature 2.5's `wisetraceloom.residency`) passes
    its own region engine here — both the commits being compacted and the
    `StorageCheckpoint` row recording that compaction must live in the same
    file the commits themselves live in, or the checkpoint would silently
    describe the wrong (or an empty) stream."""
    from wisetraceloom.storage import StorageCheckpoint, StorageCommit, get_storage_config

    resolved_engine = engine or get_engine()
    with Session(resolved_engine) as session:
        rows = session.exec(
            select(StorageCommit).where(StorageCommit.stream_id == stream_id).order_by(StorageCommit.version)
        ).all()
        if not rows:
            return None

        version = rows[-1].version
        existing = session.exec(
            select(StorageCheckpoint).where(
                StorageCheckpoint.stream_id == stream_id, StorageCheckpoint.version == version
            )
        ).first()
        if existing is not None:
            return existing

        config = get_storage_config()
        checkpoint_dir = config.checkpoint_dir or default_checkpoint_dir(resolved_engine)
        path = checkpoint_file_path(stream_id, version, checkpoint_dir, resolved_engine)
        path.parent.mkdir(parents=True, exist_ok=True)

        table = pa.Table.from_pylist(
            [
                {
                    "stream_id": row.stream_id,
                    "version": row.version,
                    "record_type": row.record_type,
                    "tenant_id": row.tenant_id,
                    "payload": row.payload,
                    "committed_at": row.committed_at.isoformat(),
                }
                for row in rows
            ]
        )
        pq.write_table(table, path)

        checkpoint = StorageCheckpoint(
            stream_id=stream_id, version=version, file_path=str(path), row_count=len(rows)
        )
        session.add(checkpoint)
        session.commit()
        session.refresh(checkpoint)
        return checkpoint


def load_checkpoint_rows(checkpoint: StorageCheckpoint) -> list[dict[str, Any]]:
    """Read a checkpoint's Parquet file back into commit-row dicts, in
    original commit order (Parquet write order is preserved by pyarrow)."""
    table = pq.read_table(checkpoint.file_path)
    return table.to_pylist()
