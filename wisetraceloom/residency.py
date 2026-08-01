"""Data-residency routing (PRD §3, §7 — feature 2.5): route a regulated
tenant's storage writes (and reads) to a physically separate, region-labeled
SQLite file instead of this SDK's single default store. PRD §3 calls out RBI
(payment systems), SEBI (securities), and IRDAI (insurance) as sector
regulators requiring India-resident storage even though DPDPA itself doesn't
mandate broad localization — "a hybrid model (India-region deployment for
regulated data, e.g. AWS ap-south-1 Mumbai) is common." This module is the
routing primitive that makes that hybrid model possible for this SDK's own
storage layer (feature 2.1); it has no AWS/cloud-provider integration of its
own — `region` is just a label the host defines and points at a file path.

**A region is a separate SQLite file, not a virtual view.** `register_region`
maps a region label to a `db_path`; `resolve_engine` returns the matching
engine (via `wisetraceloom.config.get_engine_for_path`, the same cached,
WAL-mode engine construction the default store uses) for a given tenant,
falling back to the default engine (`config.get_engine()`) when the tenant
has no region configured — residency routing is opt-in per tenant, not a
default this SDK invents. Once a tenant IS routed to a region, its stream
data lives in that region's file with its own independent
`(stream_id, version)` sequence — there is no cross-region merge. This means:

- `wisetraceloom.storage`'s `append_commit`/`enqueue_append` and
  `read_as_of_version`/`read_as_of_timestamp`/`read_latest` resolve the
  engine to use from `tenant_id` via this module, so a routed tenant's
  writes and reads consistently land in and come from the same file.
- Reading a stream **without** a `tenant_id` only ever sees the default
  store — there is no automatic fan-out across every registered region in
  this pass (that's a multi-store aggregation concern closer to the Stage 3
  viewer/SIEM-export work than this feature's scope).
- Routing a tenant to a region does **not** migrate any of that tenant's
  pre-existing data out of the default store — this is a routing rule for
  new writes going forward, not a backfill/migration tool. A tenant whose
  region config changes after it already has data in the old location needs
  an explicit migration, which this module does not attempt.

**Fail-closed, not fail-open.** `resolve_engine` raises `UnroutedRegionError`
if a tenant resolves to a region with no `register_region` call for it,
rather than silently falling back to the default store — silently storing
regulated data in the wrong (default, possibly wrong-jurisdiction) location
defeats the entire point of residency routing and is exactly the kind of
undetected violation PRD §3's regulator citations (RBI's April 2018 circular,
etc.) exist to penalize. This mirrors feature 1.4/2.2's fail-closed posture
for other compliance-critical paths, deliberately different from
instrumentation's fail-open default (feature 1.5).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, select

from wisetraceloom.config import get_db_path, get_engine, get_engine_for_path


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RegionConfig(SQLModel, table=True):
    """Which region a tenant's storage writes/reads route to. Lives in the
    default store (config tables are operational metadata, not regulated
    data themselves — only the tenant's actual stream data is what gets
    routed away)."""

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: str | None = Field(default=None, index=True)
    region: str
    updated_at: datetime = Field(default_factory=_utcnow)


# resolve_region is checked on every append_commit/read call for a given
# tenant_id — same latency hazard feature 1.3/2.1 already solved for
# ExportConfig/StorageConfig ("an uncached per-span SQLite round trip alone
# blew the Stage 1 exit gate's <5% latency budget"). Cached the same way:
# keyed by (db path, tenant_id), invalidated on every set_region_config call.
_region_config_cache: dict[tuple[str, str | None], RegionConfig | None] = {}


def get_region_config(tenant_id: str | None = None) -> RegionConfig | None:
    """Resolve region config: tenant-specific row if present, else the
    global default row, else `None` (no residency requirement configured —
    `resolve_engine` falls back to the default store)."""
    cache_key = (get_db_path(), tenant_id)
    if cache_key in _region_config_cache:
        return _region_config_cache[cache_key]

    with Session(get_engine()) as session:
        row = None
        if tenant_id is not None:
            row = session.exec(select(RegionConfig).where(RegionConfig.tenant_id == tenant_id)).first()
        if row is None:
            row = session.exec(select(RegionConfig).where(RegionConfig.tenant_id.is_(None))).first()

    _region_config_cache[cache_key] = row
    return row


def set_region_config(*, tenant_id: str | None = None, region: str) -> RegionConfig:
    """Create or update the region assignment for `tenant_id` (`None` =
    global default region every otherwise-unconfigured tenant routes to)."""
    with Session(get_engine()) as session:
        row = session.exec(select(RegionConfig).where(RegionConfig.tenant_id == tenant_id)).first()
        if row is None:
            row = RegionConfig(tenant_id=tenant_id, region=region)
            session.add(row)
        else:
            row.region = region
            row.updated_at = _utcnow()
        session.commit()
        session.refresh(row)
    _region_config_cache.clear()
    return row


# region -> db_path. Process-local, like config.py's own _db_path_override —
# no environment variables anywhere in this SDK (project-wide constraint), so
# a host registers its regions explicitly in code, typically once at startup.
_region_db_paths: dict[str, str] = {}


def register_region(region: str, db_path: str) -> None:
    """Point `region` at a SQLite file. Call before any tenant is routed to
    it — `resolve_engine` raises if a tenant resolves to a region that
    hasn't been registered yet."""
    _region_db_paths[region] = db_path


def get_region_db_path(region: str) -> str | None:
    return _region_db_paths.get(region)


class UnroutedRegionError(Exception):
    """Raised by `resolve_engine` when a tenant resolves to a region with no
    `register_region` call for it. Deliberately not a silent fallback to the
    default store — see module docstring."""


def resolve_region(tenant_id: str | None) -> str | None:
    """The region label `tenant_id` is configured to route to, or `None` if
    no residency requirement is configured for it (tenant-specific, else
    global default, else unconfigured)."""
    config = get_region_config(tenant_id)
    return config.region if config is not None else None


def resolve_engine(tenant_id: str | None) -> Engine:
    """The engine `tenant_id`'s storage writes/reads must use: the default
    shared engine if no region is configured (for this tenant or globally),
    else the registered engine for its resolved region. Raises
    `UnroutedRegionError` if a region is configured but
    `register_region` was never called for it."""
    region = resolve_region(tenant_id)
    if region is None:
        return get_engine()

    db_path = get_region_db_path(region)
    if db_path is None:
        raise UnroutedRegionError(
            f"tenant {tenant_id!r} is routed to region {region!r}, but no db path is registered for it "
            f"— call register_region({region!r}, <path>) before routing any tenant there"
        )
    return get_engine_for_path(db_path)
