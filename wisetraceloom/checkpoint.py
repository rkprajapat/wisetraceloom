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
from sqlmodel import Session, select

from wisetraceloom.config import get_db_path, get_engine

if TYPE_CHECKING:
    from wisetraceloom.storage import StorageCheckpoint

_STREAM_ID_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]")


def should_checkpoint(latest_version: int, last_checkpoint_version: int, interval: int) -> bool:
    """Pure decision function: does `latest_version` cross a new checkpoint boundary?"""
    if interval <= 0:
        return False
    return latest_version > last_checkpoint_version and latest_version % interval == 0


def default_checkpoint_dir() -> str:
    return str(Path(get_db_path()).parent / "checkpoints")


def checkpoint_file_path(stream_id: str, version: int, checkpoint_dir: str) -> Path:
    safe_stream_id = _STREAM_ID_SANITIZE_RE.sub("_", stream_id)
    return Path(checkpoint_dir) / safe_stream_id / f"{version:020d}.checkpoint.parquet"


def compact_checkpoint(stream_id: str) -> StorageCheckpoint | None:
    """Full-snapshot compaction of every commit in `stream_id` (version 1
    through the current max) into one Parquet file; upserts the
    `StorageCheckpoint` row for that `(stream_id, version)`. Returns `None`
    if the stream has no commits yet."""
    from wisetraceloom.storage import StorageCheckpoint, StorageCommit, get_storage_config

    with Session(get_engine()) as session:
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
        checkpoint_dir = config.checkpoint_dir or default_checkpoint_dir()
        path = checkpoint_file_path(stream_id, version, checkpoint_dir)
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
