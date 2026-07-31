"""Crypto-shredding for GDPR Art. 17 erasure in an immutable store (PRD §3,
§7 — feature 2.2).

The append-only commit log (`wisetraceloom.storage`, feature 2.1) is
deliberately immutable — versioned commits are never rewritten or deleted, so
tamper-evidence (feature 2.3) and time-travel reads keep working. That
conflicts with the right to erasure until PII is never stored as plaintext in
the first place: each data subject's sensitive fields are encrypted with a
**per-subject key** kept in `SubjectKey`, not baked into the ciphertext
itself, so "deleting the data" becomes "deleting the key" — the ciphertext
stays in place (hash-chain integrity computed over it survives), but is
permanently unrecoverable once its key is gone.

Two-phase workflow (PRD §3, §7; GDPR Art. 5(2) accountability). A caller
first calls `request_erasure` (status `"Requested"`), then `confirm_erasure`
(status `"Confirmed"`) as a distinct, auditable step — this mirrors the
PRD's "Requested"→"Confirmed" audit trail rather than destroying key material
as a side effect of the initial ask. `confirm_erasure` destroys every
still-active key generation for the subject and appends an immutable
erasure-fact commit to storage (who/what/when/scope) — **never the erased
plaintext or the destroyed key material itself**.

Fail-closed, not fail-open. Unlike `wisetraceloom.failsafe`'s instrumentation
wrapper (a failure to observe must never crash the host), a failure here must
never silently leave PII recoverable or an erasure unconfirmed — so this
module raises on misuse (unknown request id, double-confirm) instead of
swallowing errors.

Key generations. `SubjectKey` is versioned per subject via `generation`
(starting at 1) rather than one row per subject, because a subject can return
after erasure — `get_or_create_subject_key` provisions a fresh generation
once the prior one has no active (non-destroyed) key, so new data encrypts
under a new key while old ciphertext under the shredded generation stays
permanently unrecoverable. `encrypt_for_subject` stamps the generation used
into the returned ciphertext (`"{generation}.{fernet_token}"`) so
`decrypt_for_subject` looks up the exact key generation a token was encrypted
under, rather than always resolving "the current active key" (which may have
rotated since).

Deliberately out of scope for this pass (consistent with e.g. feature 1.6's
prompt versions): automatic wiring into `instrumentation.py`'s per-span hot
path. Spans/logs don't carry a `subject_id` field today, and not every field
in a span is subject-linked PII — a host app calls `encrypt_for_subject` /
`decrypt_for_subject` explicitly on the specific fields it knows are tied to
a data subject before/after passing them through the existing capture
pipeline (feature 1.1) or storage (feature 2.1).
"""

from __future__ import annotations

from datetime import datetime, timezone

from cryptography.fernet import Fernet
from sqlmodel import Field, Session, SQLModel, UniqueConstraint, func, select

from wisetraceloom.config import get_engine
from wisetraceloom.storage import append_commit


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SubjectKey(SQLModel, table=True):
    """One key generation for a data subject. `key_material` (a base64
    Fernet key) is set to `None` and `destroyed_at` stamped once the key is
    shredded — the row itself is kept (not deleted) so the erasure fact and
    which generation encrypted a given ciphertext remain auditable, but the
    key needed to decrypt anything under this generation is irrecoverably
    gone."""

    __table_args__ = (UniqueConstraint("subject_id", "generation", name="uq_subject_key_subject_generation"),)

    id: int | None = Field(default=None, primary_key=True)
    subject_id: str = Field(index=True)
    tenant_id: str | None = Field(default=None, index=True)
    generation: int = 1
    key_material: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    destroyed_at: datetime | None = None


class ErasureRequest(SQLModel, table=True):
    """Two-phase erasure workflow row. `status` moves `"Requested"` ->
    `"Confirmed"` only via `confirm_erasure`, never edited directly, so the
    audit trail always shows a distinct ask and a distinct confirmation."""

    id: int | None = Field(default=None, primary_key=True)
    subject_id: str = Field(index=True)
    tenant_id: str | None = Field(default=None, index=True)
    status: str = "Requested"
    scope: str | None = None
    requested_by: str | None = None
    requested_at: datetime = Field(default_factory=_utcnow)
    confirmed_at: datetime | None = None


class ErasureRequestError(Exception):
    """Raised when `confirm_erasure` is called against a missing or
    already-resolved erasure request."""


def _generate_key_material() -> str:
    return Fernet.generate_key().decode("ascii")


def get_active_subject_key(subject_id: str) -> SubjectKey | None:
    """The highest-generation row for `subject_id` that still has live key
    material, or `None` if the subject has no key yet or every generation
    has been shredded."""
    with Session(get_engine()) as session:
        return session.exec(
            select(SubjectKey)
            .where(SubjectKey.subject_id == subject_id, SubjectKey.key_material.is_not(None))
            .order_by(SubjectKey.generation.desc())
        ).first()


def get_or_create_subject_key(subject_id: str, *, tenant_id: str | None = None) -> SubjectKey:
    """Resolve `subject_id`'s active key, provisioning a new generation if
    none is active yet (first use, or every prior generation was shredded)."""
    active = get_active_subject_key(subject_id)
    if active is not None:
        return active

    with Session(get_engine()) as session:
        last_generation = session.exec(
            select(func.max(SubjectKey.generation)).where(SubjectKey.subject_id == subject_id)
        ).one()
        row = SubjectKey(
            subject_id=subject_id,
            tenant_id=tenant_id,
            generation=(last_generation or 0) + 1,
            key_material=_generate_key_material(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def encrypt_for_subject(subject_id: str, plaintext: str, *, tenant_id: str | None = None) -> str:
    """Encrypt `plaintext` under `subject_id`'s active key. Returns
    `"{generation}.{fernet_token}"` so `decrypt_for_subject` always finds the
    exact key generation this ciphertext was encrypted under, independent of
    whatever generation is active by the time it's read back."""
    key_row = get_or_create_subject_key(subject_id, tenant_id=tenant_id)
    token = Fernet(key_row.key_material.encode("ascii")).encrypt(plaintext.encode("utf-8"))
    return f"{key_row.generation}.{token.decode('ascii')}"


def decrypt_for_subject(subject_id: str, ciphertext: str) -> str | None:
    """Decrypt a token produced by `encrypt_for_subject`. Returns `None`,
    rather than raising, when the key generation the token names has been
    shredded or never existed — that is the intended, permanent outcome of
    crypto-shredding, not an error condition."""
    generation_str, _, token = ciphertext.partition(".")
    with Session(get_engine()) as session:
        row = session.exec(
            select(SubjectKey).where(
                SubjectKey.subject_id == subject_id, SubjectKey.generation == int(generation_str)
            )
        ).first()
    if row is None or row.key_material is None:
        return None
    return Fernet(row.key_material.encode("ascii")).decrypt(token.encode("ascii")).decode("utf-8")


def request_erasure(
    subject_id: str, *, tenant_id: str | None = None, requested_by: str | None = None, scope: str | None = None
) -> ErasureRequest:
    """Phase one: durably record the ask. Does not touch key material —
    `confirm_erasure` is the only thing that destroys keys."""
    with Session(get_engine()) as session:
        row = ErasureRequest(subject_id=subject_id, tenant_id=tenant_id, requested_by=requested_by, scope=scope)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def confirm_erasure(request_id: int) -> ErasureRequest:
    """Phase two: destroy every still-active key generation for the
    request's subject, mark the request `"Confirmed"`, and append an
    immutable erasure-fact commit (who/what/when/scope — never the erased
    plaintext or key material) to storage's `"erasure_log"` stream via the
    durable, synchronous `append_commit` (not the best-effort
    `enqueue_append` instrumentation uses) since a lost erasure-fact record
    would undermine the audit trail this workflow exists to provide.

    Raises `ErasureRequestError` if `request_id` doesn't exist or has already
    been confirmed — confirmation is a one-way, one-time transition."""
    with Session(get_engine()) as session:
        request = session.get(ErasureRequest, request_id)
        if request is None:
            raise ErasureRequestError(f"no erasure request with id {request_id!r}")
        if request.status != "Requested":
            raise ErasureRequestError(
                f"erasure request {request_id!r} is already {request.status!r}, not 'Requested'"
            )

        keys = session.exec(
            select(SubjectKey).where(
                SubjectKey.subject_id == request.subject_id, SubjectKey.key_material.is_not(None)
            )
        ).all()
        confirmed_at = _utcnow()
        for key in keys:
            key.key_material = None
            key.destroyed_at = confirmed_at
            session.add(key)

        request.status = "Confirmed"
        request.confirmed_at = confirmed_at
        session.add(request)
        session.commit()
        session.refresh(request)

    append_commit(
        "erasure_log",
        "erasure_fact",
        {
            "subject_id": request.subject_id,
            "erasure_request_id": request.id,
            "requested_by": request.requested_by,
            "scope": request.scope,
            "requested_at": request.requested_at.isoformat(),
            "confirmed_at": request.confirmed_at.isoformat(),
            "key_generations_destroyed": len(keys),
        },
        tenant_id=request.tenant_id,
    )
    return request
