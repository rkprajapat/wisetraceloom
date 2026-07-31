"""Tamper-evident hash chain + externally anchored Merkle roots (PRD §7 —
feature 2.3), built on the hash-chained `StorageCommit` rows `storage.py`
writes (feature 2.1's own notes flagged `prev_hash` as a deliberately
deferred gap for this pass to fill).

Kept separate from `storage.py` the same way `checkpoint.py` is: this module
groups the tamper-evidence concern (chain verification, Merkle roots,
external anchoring) on top of the write path, mirroring the existing
row-model-vs-derived-analysis split.

Two independent layers of tamper evidence:

1. **Entry-level chaining** (`verify_chain`) — every commit's `entry_hash`
   embeds the previous commit's `entry_hash` (`storage.compute_entry_hash`),
   so editing or re-ordering any single row breaks the chain from that point
   forward. Detectable entirely from this database alone.
2. **Externally anchored Merkle roots** (`anchor_commits`/`verify_anchor`) —
   PRD §7 is explicit that tamper-evidence computed and stored by the same
   operator who controls the log proves nothing on its own ("the operator
   must not control both the log and the anchor"). A Merkle root over a
   stream's full hash chain is handed to a caller-supplied `AnchorSink` — a
   callable the host wires up to something genuinely outside this database's
   control (write-once object storage in a separate account/trust domain, a
   public timestamping authority, another organization's ledger, an HSM/KMS-
   signed record — PRD §7 also calls for signing roots with keys under
   separation of duties). This module never ships a default network sink
   (no assumed infra, consistent with the SDK's no-env-vars/explicit-config
   philosophy everywhere else) — without a real external sink, "anchoring"
   would just be another row this same operator controls, which defeats the
   point. `verify_anchor` can only re-check layer 1 (local recomputation);
   verifying the external copy itself is deliberately outside this
   codebase's reach.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlmodel import Field, Session, SQLModel, select

from wisetraceloom.config import get_engine
from wisetraceloom.storage import StorageCommit, compute_entry_hash, iso_utc

# (stream_id, merkle_root) -> external_reference (whatever the external
# anchor sink returns to identify/locate the anchored record there — a
# receipt id, an object key, a transaction hash, ...).
AnchorSink = Callable[[str, str], str]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ChainVerificationResult:
    """Result of `verify_chain`. `broken_at_version` is the first version at
    which the stored chain no longer matches what's recomputed from its own
    fields — everything from there onward is suspect, regardless of whether
    later entries individually still "look" consistent."""

    ok: bool
    broken_at_version: int | None = None
    reason: str | None = None


def verify_chain(stream_id: str) -> ChainVerificationResult:
    """Walk `stream_id`'s commits in version order, recomputing each
    `entry_hash` from its own stored fields and confirming it chains onto
    the previous entry's `entry_hash` — detects both payload tampering
    (recomputed hash no longer matches what's stored) and chain re-linking
    (a `prev_hash` pointing anywhere other than the true previous entry).
    An empty stream has nothing to break, so it verifies `ok=True`."""
    with Session(get_engine()) as session:
        commits = session.exec(
            select(StorageCommit).where(StorageCommit.stream_id == stream_id).order_by(StorageCommit.version)
        ).all()

    expected_prev: str | None = None
    for commit in commits:
        if commit.prev_hash != expected_prev:
            return ChainVerificationResult(
                ok=False,
                broken_at_version=commit.version,
                reason=f"prev_hash does not match the preceding entry's entry_hash at version {commit.version}",
            )
        recomputed = compute_entry_hash(
            commit.prev_hash,
            commit.stream_id,
            commit.version,
            commit.record_type,
            commit.tenant_id,
            iso_utc(commit.committed_at),
            commit.payload,
        )
        if recomputed != commit.entry_hash:
            return ChainVerificationResult(
                ok=False,
                broken_at_version=commit.version,
                reason=f"entry_hash does not match its own recomputed fields at version {commit.version}",
            )
        expected_prev = commit.entry_hash

    return ChainVerificationResult(ok=True)


def compute_merkle_root(leaf_hashes: list[str]) -> str:
    """Standard pairwise SHA-256 Merkle root: an odd trailing leaf is
    duplicated to pair with itself (the common convention — e.g. Bitcoin's
    Merkle trees — rather than promoting it unhashed, which would let two
    different-length leaf lists collide on the same root)."""
    if not leaf_hashes:
        raise ValueError("cannot compute a Merkle root over zero leaves")

    level = list(leaf_hashes)
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        level = [
            hashlib.sha256((level[i] + level[i + 1]).encode("utf-8")).hexdigest() for i in range(0, len(level), 2)
        ]
    return level[0]


class AnchorRecord(SQLModel, table=True):
    """One externally anchored Merkle root, covering `stream_id`'s commits
    `[1..version]`. `external_reference` is whatever the `AnchorSink` this
    anchor was created with returned — this table only records that an
    anchor happened and where to go look for it externally; it is not itself
    the source of tamper-evidence (see module docstring)."""

    id: int | None = Field(default=None, primary_key=True)
    stream_id: str = Field(index=True)
    version: int
    merkle_root: str
    external_reference: str
    anchored_at: datetime = Field(default_factory=_utcnow)


def anchor_commits(stream_id: str, sink: AnchorSink) -> AnchorRecord:
    """Compute the Merkle root over every commit currently in `stream_id`,
    hand `(stream_id, root)` to `sink` for external anchoring, and persist
    the resulting `AnchorRecord`. Raises `ValueError` if the stream has no
    commits yet — there is nothing to anchor."""
    with Session(get_engine()) as session:
        commits = session.exec(
            select(StorageCommit).where(StorageCommit.stream_id == stream_id).order_by(StorageCommit.version)
        ).all()
    if not commits:
        raise ValueError(f"stream {stream_id!r} has no commits to anchor")

    root = compute_merkle_root([commit.entry_hash for commit in commits])
    external_reference = sink(stream_id, root)

    with Session(get_engine()) as session:
        record = AnchorRecord(
            stream_id=stream_id, version=commits[-1].version, merkle_root=root, external_reference=external_reference
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record


def verify_anchor(record: AnchorRecord) -> bool:
    """Recompute the Merkle root over `record.stream_id`'s commits
    `[1..record.version]` and compare against `record.merkle_root`. Only
    proves local consistency (recomputation matches what was anchored) —
    an operator who controls both this database and this function could
    tamper with both consistently, which is exactly why real tamper-evidence
    depends on checking `record.external_reference` against the actual
    external anchor, outside this codebase's reach by design."""
    with Session(get_engine()) as session:
        commits = session.exec(
            select(StorageCommit)
            .where(StorageCommit.stream_id == record.stream_id, StorageCommit.version <= record.version)
            .order_by(StorageCommit.version)
        ).all()
    if not commits or commits[-1].version != record.version:
        return False
    return compute_merkle_root([commit.entry_hash for commit in commits]) == record.merkle_root
