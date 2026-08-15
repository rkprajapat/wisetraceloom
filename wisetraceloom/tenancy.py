"""Multi-tenancy, per-tenant namespaces, and viewer RBAC (PRD §7 — feature 2.9).

Tenant ids were already a tag on spans/commits/config rows. What this module
adds is isolation and access control: a tenant's events live on their own
commit-log stream (so a query for tenant A cannot replay tenant B's
checkpoint), namespaces partition a tenant the way Langfuse partitions
environments into projects, and a membership table gates the query API the
Stage 3 viewer will call.

**Storage isolation.** `isolated_stream_id(base, tenant_id, namespace)` is
the stream-id convention (`"{base}:{tenant_id}:{namespace}"`). Colons in
ids are rejected so `acme`/`foo:bar` cannot collide with `acme:foo`/`bar`.
`wisetraceloom.instrumentation` writes tenant-tagged spans onto that stream
instead of the shared `"spans"` log feature 2.1 used; untagged spans still
land on `"spans"` so existing no-tenant call sites stay unchanged.
`append_commit` itself stays a generic primitive — it does not rewrite
`stream_id` — matching the 2.1 split where direct callers pick their stream
and only the auto-instrumented path gets the convention applied.

**Query isolation + RBAC.** `read_latest` remains ungated (storage primitive,
tests, erasure-fact reads). `query_latest(principal_id, tenant_id, ...)` is
the viewer-facing path: fail-closed `assert_viewer_access` then a read of
that tenant's isolated stream. No membership → `AccessDeniedError`, never
an empty result that could be confused with "this tenant has no data."
Unknown tenant and missing membership look the same on purpose.

**Namespaces.** Langfuse-style projects inside a tenant. `create_tenant`
mints a `"default"` namespace; further names are explicit. A membership
with `namespace=None` is tenant-wide (stored as `"*"`) and covers every
namespace; a membership naming one namespace covers only that one.

**Host-supplied principals.** There is no IdP in this SDK — `principal_id`
is an opaque string the host already authenticated. `grant_role` /
`create_tenant` are host-trusted admin APIs (same posture as
`set_prompt_alias` / `set_quota_config`); only the viewer query path is
RBAC-gated, which is what the acceptance criterion names.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlmodel import Field, Session, SQLModel, UniqueConstraint, select

from wisetraceloom.config import get_engine

DEFAULT_NAMESPACE = "default"
TENANT_WIDE = "*"
ROLES = frozenset({"owner", "admin", "viewer"})
VIEW_ROLES = ROLES
_ROLE_RANK = {"viewer": 1, "admin": 2, "owner": 3}


class TenancyError(Exception):
    """Bad input: empty id, illegal character, unknown role, duplicate tenant."""


class AccessDeniedError(Exception):
    """Viewer query refused: no matching membership for this principal."""


class Tenant(SQLModel, table=True):
    """A tenant this SDK isolates data for. `tenant_id` is the public key
    (the same string already used as `tenant_id` on spans/commits)."""

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: str = Field(unique=True, index=True)
    display_name: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Namespace(SQLModel, table=True):
    """A Langfuse-style project inside a tenant (e.g. `production` /
    `staging`). Unique per `(tenant_id, name)`."""

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_namespace_tenant_name"),)

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: str = Field(index=True)
    name: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Membership(SQLModel, table=True):
    """RBAC binding: `principal_id` may `role` on `tenant_id`/`namespace`.
    `namespace` is `TENANT_WIDE` (`"*"`) for a tenant-wide grant — stored
    as a real string so SQLite's unique constraint treats two tenant-wide
    rows as a conflict (NULL is distinct from NULL in SQLite)."""

    __table_args__ = (
        UniqueConstraint("principal_id", "tenant_id", "namespace", name="uq_membership_principal_tenant_ns"),
    )

    id: int | None = Field(default=None, primary_key=True)
    principal_id: str = Field(index=True)
    tenant_id: str = Field(index=True)
    namespace: str
    role: str
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_name(value: str, *, field: str) -> str:
    name = value.strip()
    if not name:
        raise TenancyError(f"{field} must be non-empty")
    if ":" in name:
        raise TenancyError(f"{field} must not contain ':' (used as the stream-id separator)")
    if name == TENANT_WIDE:
        raise TenancyError(f"{field} {TENANT_WIDE!r} is reserved for tenant-wide membership")
    return name


def isolated_stream_id(base: str, tenant_id: str, namespace: str | None = None) -> str:
    """Commit-log stream that holds `tenant_id`'s `namespace` (default
    `"default"`) for logical stream `base` (e.g. `"spans"`)."""
    tenant_id = _validate_name(tenant_id, field="tenant_id")
    ns = DEFAULT_NAMESPACE if namespace is None else _validate_name(namespace, field="namespace")
    base = base.strip()
    if not base:
        raise TenancyError("stream base must be non-empty")
    return f"{base}:{tenant_id}:{ns}"


def create_tenant(
    tenant_id: str, *, display_name: str | None = None, owner_principal_id: str | None = None
) -> Tenant:
    """Register `tenant_id`, mint its `default` namespace, and optionally
    grant `owner` tenant-wide to `owner_principal_id`."""
    tenant_id = _validate_name(tenant_id, field="tenant_id")
    with Session(get_engine()) as session:
        existing = session.exec(select(Tenant).where(Tenant.tenant_id == tenant_id)).first()
        if existing is not None:
            raise TenancyError(f"tenant {tenant_id!r} already exists")
        row = Tenant(tenant_id=tenant_id, display_name=display_name)
        session.add(row)
        session.add(Namespace(tenant_id=tenant_id, name=DEFAULT_NAMESPACE))
        session.commit()
        session.refresh(row)
    if owner_principal_id is not None:
        grant_role(owner_principal_id, tenant_id, "owner")
    return row


def create_namespace(tenant_id: str, name: str) -> Namespace:
    """Add a named project under `tenant_id`. Tenant must already exist."""
    tenant_id = _validate_name(tenant_id, field="tenant_id")
    name = _validate_name(name, field="namespace")
    with Session(get_engine()) as session:
        tenant = session.exec(select(Tenant).where(Tenant.tenant_id == tenant_id)).first()
        if tenant is None:
            raise TenancyError(f"unknown tenant {tenant_id!r}")
        existing = session.exec(
            select(Namespace).where(Namespace.tenant_id == tenant_id, Namespace.name == name)
        ).first()
        if existing is not None:
            raise TenancyError(f"namespace {name!r} already exists for tenant {tenant_id!r}")
        row = Namespace(tenant_id=tenant_id, name=name)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def list_namespaces(tenant_id: str) -> list[Namespace]:
    tenant_id = _validate_name(tenant_id, field="tenant_id")
    with Session(get_engine()) as session:
        return list(session.exec(select(Namespace).where(Namespace.tenant_id == tenant_id)).all())


def grant_role(
    principal_id: str, tenant_id: str, role: str, *, namespace: str | None = None
) -> Membership:
    """Upsert a membership. `namespace=None` is tenant-wide. Host-trusted
    (not itself RBAC-gated) — see module docstring."""
    principal_id = _validate_name(principal_id, field="principal_id")
    tenant_id = _validate_name(tenant_id, field="tenant_id")
    role = role.strip().lower()
    if role not in ROLES:
        raise TenancyError(f"role must be one of {sorted(ROLES)}, not {role!r}")
    stored_ns = TENANT_WIDE if namespace is None else _validate_name(namespace, field="namespace")

    with Session(get_engine()) as session:
        tenant = session.exec(select(Tenant).where(Tenant.tenant_id == tenant_id)).first()
        if tenant is None:
            raise TenancyError(f"unknown tenant {tenant_id!r}")
        if stored_ns != TENANT_WIDE:
            ns_row = session.exec(
                select(Namespace).where(Namespace.tenant_id == tenant_id, Namespace.name == stored_ns)
            ).first()
            if ns_row is None:
                raise TenancyError(f"unknown namespace {stored_ns!r} for tenant {tenant_id!r}")

        row = session.exec(
            select(Membership).where(
                Membership.principal_id == principal_id,
                Membership.tenant_id == tenant_id,
                Membership.namespace == stored_ns,
            )
        ).first()
        if row is None:
            row = Membership(
                principal_id=principal_id, tenant_id=tenant_id, namespace=stored_ns, role=role
            )
            session.add(row)
        else:
            row.role = role
            row.updated_at = _utcnow()
        session.commit()
        session.refresh(row)
        return row


def revoke_role(principal_id: str, tenant_id: str, *, namespace: str | None = None) -> None:
    """Drop the matching membership if present. Idempotent."""
    principal_id = principal_id.strip()
    tenant_id = tenant_id.strip()
    stored_ns = TENANT_WIDE if namespace is None else namespace.strip()
    if not principal_id or not tenant_id or not stored_ns:
        return
    with Session(get_engine()) as session:
        row = session.exec(
            select(Membership).where(
                Membership.principal_id == principal_id,
                Membership.tenant_id == tenant_id,
                Membership.namespace == stored_ns,
            )
        ).first()
        if row is not None:
            session.delete(row)
            session.commit()


def resolve_role(principal_id: str, tenant_id: str, namespace: str | None = None) -> str | None:
    """Highest role `principal_id` holds on `tenant_id`/`namespace`, or
    `None`. Tenant-wide and exact-namespace grants both apply; the higher
    rank wins so a namespace-scoped `viewer` cannot shadow a tenant-wide
    `owner`."""
    principal_id = principal_id.strip()
    tenant_id = tenant_id.strip()
    ns = DEFAULT_NAMESPACE if namespace is None else namespace.strip()
    if not principal_id or not tenant_id or not ns:
        return None
    with Session(get_engine()) as session:
        rows = session.exec(
            select(Membership).where(
                Membership.principal_id == principal_id,
                Membership.tenant_id == tenant_id,
                Membership.namespace.in_((ns, TENANT_WIDE)),
            )
        ).all()
    if not rows:
        return None
    return max((row.role for row in rows), key=lambda role: _ROLE_RANK.get(role, 0))


def assert_viewer_access(principal_id: str, tenant_id: str, namespace: str | None = None) -> None:
    """Fail-closed gate for viewer reads. Raises `AccessDeniedError` when
    `principal_id` has no view-capable role on this tenant/namespace."""
    role = resolve_role(principal_id, tenant_id, namespace)
    if role not in VIEW_ROLES:
        raise AccessDeniedError(
            f"principal {principal_id!r} cannot view tenant {tenant_id!r} namespace "
            f"{(namespace or DEFAULT_NAMESPACE)!r}"
        )


def query_latest(
    principal_id: str,
    tenant_id: str,
    *,
    namespace: str | None = None,
    stream_base: str = "spans",
) -> list[dict[str, Any]]:
    """Viewer-facing read: RBAC check, then `read_latest` on the tenant's
    isolated stream. `namespace=None` means `default`."""
    ns = DEFAULT_NAMESPACE if namespace is None else namespace
    assert_viewer_access(principal_id, tenant_id, ns)
    from wisetraceloom.storage import read_latest

    return read_latest(isolated_stream_id(stream_base, tenant_id, ns), tenant_id=tenant_id)
