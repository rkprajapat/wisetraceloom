"""Delta-Lake-inspired append-only storage (PRD §1, §5 — feature 2.1).

A new, parallel persistence path alongside structured logging (feature 1.1)
and OTel export (feature 1.3) — it does not replace either. Every write is an
immutable, versioned **commit** appended to a named **stream** (e.g.
`"spans"`); reads reconstruct a stream's state as of a given version or
timestamp by replaying commits, optionally starting from a periodic Parquet
**checkpoint** (`wisetraceloom.checkpoint`) instead of the full history.

Rows, not files: Delta Lake's JSON-commit-files-on-object-storage design
exists to fake atomic create-if-not-exists on storage with no native
transactions. SQLite already has real transactions and unique constraints, so
the commit log here is a generic SQLite table — `stream_id`/`record_type`/
`payload` stay deliberately generic (not span-specific) so a future stream
(e.g. `prompt_versions:{slot}`, PRD §8.4) needs no schema change, just a new
`stream_id` convention. This is an event-log replay model (every commit is an
immutable event, not a keyed upsert) — correct and simplest for spans; a
keyed-latest stream would layer its own reduction on top of these generic
read primitives.

Optimistic concurrency control: `append_commit` computes the next version as
an indexed `max(version)` lookup (not `len(prior_rows) + 1` like
`wisetraceloom.prompts.PromptVersion` uses — fine for a handful of prompt
versions, wrong at commit-log volume) and retries with a fresh version number
on conflict. A `(stream_id, version)` unique constraint plus WAL mode + a
busy timeout on the shared engine (`wisetraceloom.config._get_engine`) make
this hold under real concurrent writers, not just single-threaded tests.

Two write paths, one durability trade-off. `append_commit` is synchronous —
it blocks until the row is durably committed (or raises `StorageConflictError`)
and is what the OCC/time-travel acceptance criteria are tested against
directly. But even a fast SQLAlchemy-mediated SQLite write costs a few
milliseconds — trivial next to a real LLM/tool call, but enough to blow the
Stage 1 exit gate's <5% latency budget (feature 1.9) if it sits inline on
every span. So `instrumentation.py`'s per-span hot path calls
`enqueue_append` instead: a non-blocking, best-effort handoff to a background
writer thread that calls the same `append_commit` off the caller's critical
path. This mirrors the PRD's own gap analysis (§7: "local buffering/spooling
when the backend is unavailable... bounded queues with an explicit drop
policy") and this SDK's existing fail-open philosophy — instrumentation
storage is best-effort, never a source of added latency or crashes for the
host. Direct callers (tests, a future prompt-version migration) that need a
durable, conflict-checked write use `append_commit` directly.
"""

from __future__ import annotations

import hashlib
import json
import queue
import random
import threading
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Field, Session, SQLModel, UniqueConstraint, func, select

from wisetraceloom.config import get_db_path, get_engine
from wisetraceloom.logging import get_logger
from wisetraceloom.residency import resolve_engine


def iso_utc(dt: datetime) -> str:
    """`dt.isoformat()`, normalized to UTC-aware first. SQLite round-trips a
    `datetime` via SQLAlchemy as **naive** (see module note) even when the
    value written was timezone-aware, so a freshly-read-back `committed_at`
    would otherwise `.isoformat()` differently (no `+00:00` suffix) than the
    same instant did at write time — silently breaking every entry's hash
    chain on the very next read. Every column in this module is written as
    `datetime.now(timezone.utc)`, so treating a naive value as UTC here is
    recovering the original meaning, not guessing it."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def compute_entry_hash(
    prev_hash: str | None,
    stream_id: str,
    version: int,
    record_type: str,
    tenant_id: str | None,
    committed_at_iso: str,
    payload_json: str,
) -> str:
    """SHA-256 over `prev_hash` plus every field a `StorageCommit` row
    carries — the hash-chaining primitive feature 2.3 builds on. A shared,
    pure function (rather than inlining the formula in `append_commit`) so
    `wisetraceloom.audit_chain`'s `verify_chain` recomputes it identically
    when checking a stream for tampering; the two must never drift apart."""
    material = "\n".join(
        [prev_hash or "", stream_id, str(version), record_type, tenant_id or "", committed_at_iso, payload_json]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class StorageCommit(SQLModel, table=True):
    """One immutable, versioned event appended to `stream_id`. `version` is
    monotonic per stream, starting at 1, with no gaps — the unique
    constraint guarantees exactly-once assignment per version.

    `prev_hash`/`entry_hash` form the tamper-evident hash chain (PRD §7,
    feature 2.3): `entry_hash` is `compute_entry_hash` over this row's own
    fields plus `prev_hash`, and `prev_hash` is the prior version's
    `entry_hash` (`None` for a stream's first commit) — so re-linking or
    editing any single entry breaks the chain from that point forward,
    detectable by `wisetraceloom.audit_chain.verify_chain` without needing a
    separate ledger."""

    __table_args__ = (UniqueConstraint("stream_id", "version", name="uq_storage_commit_stream_version"),)

    id: int | None = Field(default=None, primary_key=True)
    stream_id: str = Field(index=True)
    version: int = Field(index=True)
    record_type: str
    tenant_id: str | None = Field(default=None, index=True)
    payload: str
    committed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)
    prev_hash: str | None = Field(default=None)
    entry_hash: str = Field(default="", index=True)


class StorageCheckpoint(SQLModel, table=True):
    """Metadata for a full-snapshot Parquet compaction of `stream_id`
    covering commits `[1..version]` — not a delta."""

    __table_args__ = (UniqueConstraint("stream_id", "version", name="uq_storage_checkpoint_stream_version"),)

    id: int | None = Field(default=None, primary_key=True)
    stream_id: str = Field(index=True)
    version: int = Field(index=True)
    file_path: str
    row_count: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StorageConfig(SQLModel, table=True):
    """Checkpoint/retention knobs. `tenant_id` follows the rest of the
    codebase's nullable-tenant-fallback convention (see
    `wisetraceloom.config.RotationConfig`) but is currently inert — this
    pass uses a single global `"spans"` stream shared across tenants, so
    per-tenant checkpoint config has nothing to attach to yet."""

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: str | None = Field(default=None, index=True)
    checkpoint_interval_commits: int = 10
    # No pruning job reads this yet (retention enforcement is out of scope
    # for this pass); the field exists so the knob doesn't need a migration
    # to add later.
    retention_days: int | None = 30
    checkpoint_dir: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StorageConflictError(Exception):
    """Raised when `append_commit` exhausts its retries on version conflicts."""


# checkpoint_interval_commits is checked on every single append_commit call,
# same hazard feature 1.3 hit with ExportConfig ("an uncached per-span SQLite
# round trip alone blew the Stage 1 exit gate's <5% latency budget, even with
# export disabled") — cached in-process the same way, keyed by (db path,
# tenant_id), invalidated on every set_storage_config write.
_storage_config_cache: dict[tuple[str, str | None], StorageConfig] = {}

# Last-known checkpoint version per stream, cached in-process so the
# checkpoint-due check on every append_commit doesn't need its own SQLite
# round trip. Keyed by (db path, stream_id); lazily populated with one query
# the first time a stream is seen in this process, then advanced in memory
# whenever a checkpoint actually fires — never re-queried on the fast path.
_last_checkpoint_version_cache: dict[tuple[str, str], int] = {}


def _load_storage_config(tenant_id: str | None) -> StorageConfig:
    with Session(get_engine()) as session:
        if tenant_id is not None:
            row = session.exec(select(StorageConfig).where(StorageConfig.tenant_id == tenant_id)).first()
            if row is not None:
                return row
        row = session.exec(select(StorageConfig).where(StorageConfig.tenant_id.is_(None))).first()
        if row is not None:
            return row
    return StorageConfig(checkpoint_interval_commits=10, retention_days=30)


def get_storage_config(tenant_id: str | None = None) -> StorageConfig:
    """Resolve storage config: tenant-specific row if present, else the
    global default row, else a built-in (not persisted) default."""
    cache_key = (get_db_path(), tenant_id)
    cached = _storage_config_cache.get(cache_key)
    if cached is not None:
        return cached
    row = _load_storage_config(tenant_id)
    _storage_config_cache[cache_key] = row
    return row


def set_storage_config(
    *,
    tenant_id: str | None = None,
    checkpoint_interval_commits: int = 10,
    retention_days: int | None = 30,
    checkpoint_dir: str | None = None,
) -> StorageConfig:
    """Create or update the storage config row for `tenant_id` (None = global default)."""
    with Session(get_engine()) as session:
        row = session.exec(select(StorageConfig).where(StorageConfig.tenant_id == tenant_id)).first()
        if row is None:
            row = StorageConfig(tenant_id=tenant_id)
            session.add(row)
        row.checkpoint_interval_commits = checkpoint_interval_commits
        row.retention_days = retention_days
        row.checkpoint_dir = checkpoint_dir
        row.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(row)
        _storage_config_cache.clear()
        return row


def _next_version(session: Session, stream_id: str) -> int:
    max_version = session.exec(
        select(func.max(StorageCommit.version)).where(StorageCommit.stream_id == stream_id)
    ).one()
    return (max_version or 0) + 1


def _prev_entry_hash(session: Session, stream_id: str, version: int) -> str | None:
    """The prior version's `entry_hash` to chain onto, or `None` for a
    stream's first commit. Queried fresh (not cached) every append —
    SQLite's single-writer serialization guarantees version `V - 1` is
    already durably committed by the time any writer succeeds at version `V`
    (see `append_commit`'s OCC retry loop), so this is always exactly one
    row or none, never a race; correctness of the hash chain matters more
    here than the one extra query costs."""
    if version == 1:
        return None
    return session.exec(
        select(StorageCommit.entry_hash).where(StorageCommit.stream_id == stream_id, StorageCommit.version == version - 1)
    ).one()


def append_commit(
    stream_id: str,
    record_type: str,
    payload: dict[str, Any],
    *,
    tenant_id: str | None = None,
    max_retries: int = 20,
    engine: Engine | None = None,
) -> StorageCommit:
    """Append `payload` as the next version in `stream_id`. Retries with a
    freshly computed version number on a version conflict — `IntegrityError`
    is a real OCC conflict (another writer took the version); `OperationalError`
    is SQLite lock contention under concurrent writers, retried the same way.
    Raises `StorageConflictError` if `max_retries` is exhausted.

    `max_retries` defaults higher than feature 2.1's original 5: feature
    2.3's `_prev_entry_hash` lookup added a second read per attempt, widening
    the window in which many concurrent writers can all observe the same
    stale "next version" before any of them commits — under a thundering
    herd (many writers starting at once, e.g. this module's own concurrency
    tests), that occasionally needed more than 5 rounds to resolve even
    though no writer was ever actually lost, just contending. The small
    jittered backoff below (rather than retrying instantly in a tight loop)
    is the standard fix for that: it desynchronizes retriers so each round
    resolves more of them, keeping the *typical* case just as fast as before
    (no contention -> zero retries, zero sleep) while making the
    worst-case thundering-herd path actually converge instead of exhausting
    a small fixed retry budget.

    `engine` defaults to resolving from `tenant_id` via
    `wisetraceloom.residency` (feature 2.5) — a tenant routed to a region
    writes to that region's own SQLite file instead of the default store.
    `enqueue_append` passes an already-resolved `engine` explicitly instead
    of leaving this function to resolve it: resolution reads ambient config
    (`RegionConfig`, `get_db_path()`), and `enqueue_append`'s whole point is
    handing the write to a background thread that may not run until well
    after the enqueuing call returns — resolving lazily on that thread would
    use whatever config happens to be active *then*, not what was active
    when the write was actually requested (in tests, this manifested as a
    background write silently landing in a stale, differently-schema'd
    database once an earlier test's config override had already been torn
    down by the time the queue drained)."""
    engine = engine or resolve_engine(tenant_id)
    payload_json = json.dumps(payload, sort_keys=True, default=str)

    for attempt in range(max_retries):
        with Session(engine) as session:
            version = _next_version(session, stream_id)
            prev_hash = _prev_entry_hash(session, stream_id, version)
            committed_at = datetime.now(timezone.utc)
            entry_hash = compute_entry_hash(
                prev_hash, stream_id, version, record_type, tenant_id, iso_utc(committed_at), payload_json
            )
            commit = StorageCommit(
                stream_id=stream_id,
                version=version,
                record_type=record_type,
                tenant_id=tenant_id,
                payload=payload_json,
                committed_at=committed_at,
                prev_hash=prev_hash,
                entry_hash=entry_hash,
            )
            session.add(commit)
            try:
                session.commit()
            except (IntegrityError, OperationalError):
                session.rollback()
                time.sleep(random.uniform(0, 0.001 * (attempt + 1)))
                continue
            session.refresh(commit)
            _maybe_checkpoint(stream_id, version, engine)
            return commit

    raise StorageConflictError(
        f"append to stream {stream_id!r} failed after {max_retries} version-conflict retries"
    )


# Bounded queue + single background writer thread for enqueue_append's
# best-effort, non-blocking handoff (see module docstring). Bounded so a
# stalled writer degrades by dropping new writes (logged) rather than
# growing memory without limit — an explicit drop policy per PRD §7.
_write_queue: "queue.Queue[tuple[str, str, dict[str, Any], str | None, Engine]]" = queue.Queue(maxsize=10_000)
_write_worker_thread: threading.Thread | None = None
_write_worker_lock = threading.Lock()


def _write_worker_loop() -> None:
    while True:
        stream_id, record_type, payload, tenant_id, engine = _write_queue.get()
        try:
            append_commit(stream_id, record_type, payload, tenant_id=tenant_id, engine=engine)
        except Exception:
            get_logger("wisetraceloom.storage").warning(
                "wisetraceloom_async_append_failed", stream_id=stream_id, record_type=record_type
            )
        finally:
            _write_queue.task_done()


def _ensure_write_worker() -> None:
    global _write_worker_thread
    if _write_worker_thread is not None and _write_worker_thread.is_alive():
        return
    with _write_worker_lock:
        if _write_worker_thread is not None and _write_worker_thread.is_alive():
            return
        thread = threading.Thread(target=_write_worker_loop, daemon=True, name="wisetraceloom-storage-writer")
        _write_worker_thread = thread
        thread.start()


def enqueue_append(
    stream_id: str, record_type: str, payload: dict[str, Any], *, tenant_id: str | None = None
) -> None:
    """Non-blocking, best-effort append: hands the commit to a background
    writer thread instead of durably writing inline (see module docstring
    for the durability trade-off). If the queue is full, the write is
    dropped and logged rather than blocking the caller.

    The target engine is resolved from `tenant_id` right here, synchronously,
    rather than left for the background thread to resolve when it eventually
    dequeues this write — see `append_commit`'s docstring for why resolving
    late reads whatever config is ambient *then*, not what was ambient when
    the write was actually requested."""
    _ensure_write_worker()
    engine = resolve_engine(tenant_id)
    try:
        _write_queue.put_nowait((stream_id, record_type, payload, tenant_id, engine))
    except queue.Full:
        get_logger("wisetraceloom.storage").warning(
            "wisetraceloom_storage_queue_full", stream_id=stream_id, record_type=record_type
        )


def wait_for_pending_writes() -> None:
    """Block until the background writer has drained every commit queued via
    `enqueue_append` so far. Not needed for correctness — for tests/callers
    that want to deterministically observe an async-enqueued write having
    landed. Does not include checkpoint compaction (see
    `wait_for_pending_checkpoints`)."""
    _write_queue.join()


def _load_last_checkpoint_version(stream_id: str, engine: Engine) -> int:
    with Session(engine) as session:
        version = session.exec(
            select(func.max(StorageCheckpoint.version)).where(StorageCheckpoint.stream_id == stream_id)
        ).one()
    return version or 0


# Parquet compaction (schema setup, columnar encoding, file I/O) costs several
# ms per checkpoint — trivial compared to a 50ms LLM/tool call, but enough to
# blow the Stage 1 exit gate's <5% latency budget (feature 1.9) if it runs
# synchronously inside append_commit. Checkpoints are self-healing by design
# (each one is a full snapshot, not a delta; reads reconcile checkpoint+tail
# regardless of exactly when the checkpoint lands), so compaction runs on a
# background thread instead — the OCC commit itself (the durable, correctness-
# critical part) stays fully synchronous.
_pending_checkpoint_threads: list[threading.Thread] = []
_pending_checkpoint_threads_lock = threading.Lock()


def wait_for_pending_checkpoints(timeout: float = 5.0) -> None:
    """Block until all in-flight background checkpoint compactions finish.
    Not needed for correctness (reads reconcile checkpoint+tail regardless of
    timing) — for tests/callers that want to deterministically observe a
    checkpoint having landed."""
    with _pending_checkpoint_threads_lock:
        threads = list(_pending_checkpoint_threads)
        _pending_checkpoint_threads.clear()
    for thread in threads:
        thread.join(timeout=timeout)


def _spawn_checkpoint(stream_id: str, cache_key: tuple[str, str], latest_version: int, engine: Engine) -> None:
    from wisetraceloom import checkpoint as checkpoint_module

    def _compact() -> None:
        try:
            checkpoint_module.compact_checkpoint(stream_id, engine=engine)
            _last_checkpoint_version_cache[cache_key] = latest_version
        except Exception:
            # Failure is left unadvanced in the cache so the next interval's
            # check still sees the true last-successful checkpoint version
            # and retries a full (self-healing) snapshot then.
            get_logger("wisetraceloom.storage").warning(
                "wisetraceloom_checkpoint_failed", stream_id=stream_id, version=latest_version
            )

    thread = threading.Thread(target=_compact, daemon=True, name=f"wisetraceloom-checkpoint-{stream_id}")
    with _pending_checkpoint_threads_lock:
        _pending_checkpoint_threads.append(thread)
    thread.start()


def _maybe_checkpoint(stream_id: str, latest_version: int, engine: Engine) -> None:
    from wisetraceloom import checkpoint as checkpoint_module

    config = get_storage_config()
    # Keyed by the engine's own URL, not get_db_path() — a region-routed
    # stream (feature 2.5) lives in a different file than the default
    # store, and the checkpoint-version cache must track each file's own
    # checkpoint history independently, not conflate them.
    cache_key = (str(engine.url), stream_id)
    last_checkpoint_version = _last_checkpoint_version_cache.get(cache_key)
    if last_checkpoint_version is None:
        last_checkpoint_version = _load_last_checkpoint_version(stream_id, engine)
        _last_checkpoint_version_cache[cache_key] = last_checkpoint_version

    if not checkpoint_module.should_checkpoint(
        latest_version, last_checkpoint_version, config.checkpoint_interval_commits
    ):
        return

    _spawn_checkpoint(stream_id, cache_key, latest_version, engine)


def _load_stream_state(
    stream_id: str, target_version: int, *, tenant_id: str | None = None, engine: Engine | None = None
) -> list[dict[str, Any]]:
    from wisetraceloom import checkpoint as checkpoint_module

    with Session(engine or get_engine()) as session:
        best_checkpoint = session.exec(
            select(StorageCheckpoint)
            .where(StorageCheckpoint.stream_id == stream_id, StorageCheckpoint.version <= target_version)
            .order_by(StorageCheckpoint.version.desc())
        ).first()

        base_version = 0
        rows: list[dict[str, Any]] = []
        if best_checkpoint is not None:
            base_version = best_checkpoint.version
            rows.extend(checkpoint_module.load_checkpoint_rows(best_checkpoint))

        tail = session.exec(
            select(StorageCommit)
            .where(
                StorageCommit.stream_id == stream_id,
                StorageCommit.version > base_version,
                StorageCommit.version <= target_version,
            )
            .order_by(StorageCommit.version)
        ).all()
        rows.extend(
            {
                "stream_id": commit.stream_id,
                "version": commit.version,
                "record_type": commit.record_type,
                "tenant_id": commit.tenant_id,
                "payload": commit.payload,
                "committed_at": commit.committed_at.isoformat()
                if isinstance(commit.committed_at, datetime)
                else commit.committed_at,
            }
            for commit in tail
        )

    rows.sort(key=lambda row: row["version"])
    if tenant_id is not None:
        rows = [row for row in rows if row["tenant_id"] == tenant_id]
    return rows


def read_as_of_version(stream_id: str, version: int, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
    """Ordered list of commit payloads (`json.loads`'d) for `stream_id` with
    `version <= version`, reconciling the latest applicable checkpoint plus
    the commits since it.

    The engine queried is resolved from `tenant_id` (feature 2.5's
    `wisetraceloom.residency`) the same way `append_commit` resolves it for
    writes — a region-routed tenant's reads come from that region's file.
    Without a `tenant_id`, only the default store is queried; there is no
    fan-out across every registered region (see `residency` module
    docstring)."""
    engine = resolve_engine(tenant_id) if tenant_id is not None else get_engine()
    rows = _load_stream_state(stream_id, version, tenant_id=tenant_id, engine=engine)
    return [json.loads(row["payload"]) for row in rows]


def read_as_of_timestamp(stream_id: str, timestamp: datetime, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
    """Resolve `timestamp` to the highest commit version with
    `committed_at <= timestamp` for `stream_id`, then delegate to
    `read_as_of_version`. Returns `[]` if no commit exists at or before
    `timestamp`."""
    engine = resolve_engine(tenant_id) if tenant_id is not None else get_engine()
    with Session(engine) as session:
        version = session.exec(
            select(func.max(StorageCommit.version)).where(
                StorageCommit.stream_id == stream_id, StorageCommit.committed_at <= timestamp
            )
        ).one()
    if version is None:
        return []
    return read_as_of_version(stream_id, version, tenant_id=tenant_id)


def read_latest(stream_id: str, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
    """All commit payloads for `stream_id`, in version order."""
    engine = resolve_engine(tenant_id) if tenant_id is not None else get_engine()
    with Session(engine) as session:
        version = session.exec(
            select(func.max(StorageCommit.version)).where(StorageCommit.stream_id == stream_id)
        ).one()
    if version is None:
        return []
    return read_as_of_version(stream_id, version, tenant_id=tenant_id)
