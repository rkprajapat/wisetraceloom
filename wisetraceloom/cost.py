"""Per-tenant cost attribution + quota kill-switches (PRD §7 — feature 2.4).

**Attribution, not invoices.** PRD §7 is explicit: attribute cost from spans
(this SDK's own `LLMSpan.input_tokens`/`output_tokens`/etc., feature 1.2),
not provider invoices, which are aggregated, delayed, and untagged by
tenant. `estimate_cost_usd` turns a span's already-captured token counts
into a dollar figure using a per-tenant-overridable `PricingConfig` table
(same tenant-specific -> global-default -> unresolved fallback convention as
every other config domain in this codebase); `wisetraceloom.instrumentation`'s
`llm_call` auto-fills `LLMSpan.estimated_cost_usd` from this when the host
hasn't already set it, then attributes the resulting cost to the span's
tenant via `record_spend` — all wrapped in `fail_open_context` there, since
cost attribution is instrumentation, not business logic (feature 1.5's
fail-open boundary applies here exactly as it does to span emission).

**Daily spend cap ("kill-switch") is a harness-layer primitive, not
automatic gating inside `llm_call`.** PRD §7 asks to "tag tenant/user/task
IDs at request creation time in the harness/wrapper layer" — the kill switch
this module provides (`assert_within_quota`) is meant to be called there,
*before* the host's own code makes the actual provider call, so a
quota-exceeded tenant's request never reaches the provider at all. Wiring
this automatically into `llm_call`'s `__enter__` was deliberately rejected:
`llm_call` wraps a call the host has already decided to make, and this
SDK's core fail-open guarantee (feature 1.5) is that its own instrumentation
never blocks the host's business logic — a quota kill-switch doing exactly
that, silently, from inside an unrelated context manager, would undermine
that guarantee for every other caller who didn't ask for quota enforcement.
Cost *attribution* (this module auto-doing) and cost *enforcement* (the host
explicitly calling `assert_within_quota`) are kept as separate opt-in steps
for this reason.

**7-day rolling baseline alerting** (`check_spend_anomaly`) has no default
alerting sink, mirroring feature 2.3's anchor-sink design: this module
doesn't assume PagerDuty/Slack/email infra exists, so an anomalous day emits
a structured `wisetraceloom_spend_anomaly` warning log event (feature 1.1's
pipeline) that a host wires its own alerting onto, rather than this SDK
attempting a network call of its own.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Field, Session, SQLModel, UniqueConstraint, select

from wisetraceloom.config import get_engine
from wisetraceloom.logging import get_logger


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _today(as_of: date | None) -> date:
    return as_of if as_of is not None else datetime.now(timezone.utc).date()


class PricingConfig(SQLModel, table=True):
    """USD cost per 1,000 tokens for one `(provider_name, request_model)`,
    optionally overridden per tenant. Token layers mirror `schema.LLMSpan`:
    prompt/response priced separately from cache read/creation, since
    providers price those differently."""

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: str | None = Field(default=None, index=True)
    provider_name: str = Field(index=True)
    request_model: str = Field(index=True)
    input_cost_per_1k_usd: float = 0.0
    output_cost_per_1k_usd: float = 0.0
    cache_read_cost_per_1k_usd: float = 0.0
    cache_creation_cost_per_1k_usd: float = 0.0
    updated_at: datetime = Field(default_factory=_utcnow)


def get_pricing_config(provider_name: str, request_model: str, *, tenant_id: str | None = None) -> PricingConfig | None:
    """Resolve pricing: tenant-specific row if present, else the global
    default row, else `None` (no pricing known — callers can't estimate
    cost for a provider/model pair they haven't configured, and this module
    never guesses)."""
    with Session(get_engine()) as session:
        if tenant_id is not None:
            row = session.exec(
                select(PricingConfig).where(
                    PricingConfig.tenant_id == tenant_id,
                    PricingConfig.provider_name == provider_name,
                    PricingConfig.request_model == request_model,
                )
            ).first()
            if row is not None:
                return row
        return session.exec(
            select(PricingConfig).where(
                PricingConfig.tenant_id.is_(None),
                PricingConfig.provider_name == provider_name,
                PricingConfig.request_model == request_model,
            )
        ).first()


def set_pricing_config(
    provider_name: str,
    request_model: str,
    *,
    tenant_id: str | None = None,
    input_cost_per_1k_usd: float = 0.0,
    output_cost_per_1k_usd: float = 0.0,
    cache_read_cost_per_1k_usd: float = 0.0,
    cache_creation_cost_per_1k_usd: float = 0.0,
) -> PricingConfig:
    """Create or update the pricing row for `(tenant_id, provider_name, request_model)`."""
    with Session(get_engine()) as session:
        row = session.exec(
            select(PricingConfig).where(
                PricingConfig.tenant_id == tenant_id,
                PricingConfig.provider_name == provider_name,
                PricingConfig.request_model == request_model,
            )
        ).first()
        if row is None:
            row = PricingConfig(tenant_id=tenant_id, provider_name=provider_name, request_model=request_model)
            session.add(row)
        row.input_cost_per_1k_usd = input_cost_per_1k_usd
        row.output_cost_per_1k_usd = output_cost_per_1k_usd
        row.cache_read_cost_per_1k_usd = cache_read_cost_per_1k_usd
        row.cache_creation_cost_per_1k_usd = cache_creation_cost_per_1k_usd
        row.updated_at = _utcnow()
        session.commit()
        session.refresh(row)
        return row


def estimate_cost_usd(
    provider_name: str,
    request_model: str,
    *,
    tenant_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_input_tokens: int | None = None,
    cache_creation_input_tokens: int | None = None,
) -> float | None:
    """USD cost from token counts using the resolved `PricingConfig`.
    Returns `None` (not 0.0) if no pricing is configured for this
    provider/model — "unknown" and "free" are different things, and
    silently reporting $0 for an unconfigured model would misattribute
    spend rather than just leave it unattributed."""
    pricing = get_pricing_config(provider_name, request_model, tenant_id=tenant_id)
    if pricing is None:
        return None
    return (
        (input_tokens or 0) / 1000 * pricing.input_cost_per_1k_usd
        + (output_tokens or 0) / 1000 * pricing.output_cost_per_1k_usd
        + (cache_read_input_tokens or 0) / 1000 * pricing.cache_read_cost_per_1k_usd
        + (cache_creation_input_tokens or 0) / 1000 * pricing.cache_creation_cost_per_1k_usd
    )


class QuotaConfig(SQLModel, table=True):
    """Daily spend cap for a tenant (or the global default, `tenant_id=None`)."""

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: str | None = Field(default=None, index=True)
    daily_cap_usd: float
    updated_at: datetime = Field(default_factory=_utcnow)


def get_quota_config(tenant_id: str | None = None) -> QuotaConfig | None:
    """Tenant-specific cap if present, else the global default, else `None`
    (no cap configured -> `assert_within_quota` never trips)."""
    with Session(get_engine()) as session:
        if tenant_id is not None:
            row = session.exec(select(QuotaConfig).where(QuotaConfig.tenant_id == tenant_id)).first()
            if row is not None:
                return row
        return session.exec(select(QuotaConfig).where(QuotaConfig.tenant_id.is_(None))).first()


def set_quota_config(*, tenant_id: str | None = None, daily_cap_usd: float) -> QuotaConfig:
    """Create or update the daily spend cap for `tenant_id` (`None` = global default)."""
    with Session(get_engine()) as session:
        row = session.exec(select(QuotaConfig).where(QuotaConfig.tenant_id == tenant_id)).first()
        if row is None:
            row = QuotaConfig(tenant_id=tenant_id, daily_cap_usd=daily_cap_usd)
            session.add(row)
        else:
            row.daily_cap_usd = daily_cap_usd
            row.updated_at = _utcnow()
        session.commit()
        session.refresh(row)
        return row


class TenantDailySpend(SQLModel, table=True):
    """Running total spend for one tenant on one UTC calendar day
    (`spend_date`, ISO `YYYY-MM-DD`). Incremented via an atomic SQLite
    upsert (`record_spend`), not a read-modify-write — concurrent LLM calls
    for the same tenant on the same day are the expected common case, and a
    read-then-write here would lose updates under real concurrency the same
    way a naive counter would (see `record_spend`'s docstring)."""

    __table_args__ = (UniqueConstraint("tenant_id", "spend_date", name="uq_tenant_daily_spend_tenant_date"),)

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: str = Field(index=True)
    spend_date: str = Field(index=True)
    total_usd: float = 0.0
    updated_at: datetime = Field(default_factory=_utcnow)


def record_spend(tenant_id: str, amount_usd: float, *, as_of: date | None = None) -> TenantDailySpend:
    """Atomically add `amount_usd` to `tenant_id`'s running total for
    `as_of`'s UTC date (today if unset), via SQLite's native
    `INSERT ... ON CONFLICT DO UPDATE` rather than a separate select-then-
    update — the latter races under concurrent callers incrementing the
    same `(tenant_id, spend_date)` row (two readers see the same starting
    total, both add their delta, one update overwrites the other), which a
    single atomic statement can't."""
    spend_date = _today(as_of).isoformat()
    now = _utcnow()
    table = TenantDailySpend.__table__
    stmt = sqlite_insert(table).values(
        tenant_id=tenant_id, spend_date=spend_date, total_usd=amount_usd, updated_at=now
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["tenant_id", "spend_date"],
        set_={"total_usd": table.c.total_usd + amount_usd, "updated_at": now},
    )
    with Session(get_engine()) as session:
        session.execute(stmt)
        session.commit()
        return session.exec(
            select(TenantDailySpend).where(
                TenantDailySpend.tenant_id == tenant_id, TenantDailySpend.spend_date == spend_date
            )
        ).one()


def get_daily_spend(tenant_id: str, *, as_of: date | None = None) -> float:
    """`tenant_id`'s running total for `as_of`'s UTC date (today if unset);
    `0.0` if nothing has been recorded yet."""
    spend_date = _today(as_of).isoformat()
    with Session(get_engine()) as session:
        row = session.exec(
            select(TenantDailySpend).where(
                TenantDailySpend.tenant_id == tenant_id, TenantDailySpend.spend_date == spend_date
            )
        ).first()
    return row.total_usd if row is not None else 0.0


class QuotaExceededError(Exception):
    """Raised by `assert_within_quota` when a tenant's spend for the day is
    at or above its configured daily cap."""


def assert_within_quota(tenant_id: str, *, as_of: date | None = None) -> None:
    """The kill-switch: call this in the harness/wrapper layer *before*
    making the actual provider call. Raises `QuotaExceededError` if
    `tenant_id`'s spend for the day is already at or above its resolved
    daily cap. A no-op (never raises) if no cap is configured for this
    tenant or globally — quota enforcement is opt-in, not a default limit
    this SDK invents on the host's behalf."""
    quota = get_quota_config(tenant_id)
    if quota is None:
        return
    spent = get_daily_spend(tenant_id, as_of=as_of)
    if spent >= quota.daily_cap_usd:
        raise QuotaExceededError(
            f"tenant {tenant_id!r} has spent ${spent:.4f} today, at or above its ${quota.daily_cap_usd:.4f} daily cap"
        )


def get_rolling_baseline(tenant_id: str, *, as_of: date | None = None, window_days: int = 7) -> float | None:
    """Average daily spend over the `window_days` UTC calendar days
    immediately before `as_of` (today if unset) — `as_of` itself is
    excluded, since the point is comparing today against a baseline of
    prior days, not against itself. Returns `None` if no spend is on record
    for any day in the window (nothing to compare against yet)."""
    today = _today(as_of)
    window_dates = {(today - timedelta(days=offset)).isoformat() for offset in range(1, window_days + 1)}
    with Session(get_engine()) as session:
        rows = session.exec(
            select(TenantDailySpend).where(
                TenantDailySpend.tenant_id == tenant_id, TenantDailySpend.spend_date.in_(window_dates)
            )
        ).all()
    if not rows:
        return None
    return sum(row.total_usd for row in rows) / window_days


class SpendAnomalyResult:
    """Result of `check_spend_anomaly`."""

    def __init__(self, *, is_anomalous: bool, today_spend: float, baseline: float | None, threshold_multiplier: float):
        self.is_anomalous = is_anomalous
        self.today_spend = today_spend
        self.baseline = baseline
        self.threshold_multiplier = threshold_multiplier


def check_spend_anomaly(
    tenant_id: str, *, as_of: date | None = None, window_days: int = 7, threshold_multiplier: float = 2.0
) -> SpendAnomalyResult:
    """Flag `tenant_id` as anomalous if today's spend exceeds
    `threshold_multiplier` times its `window_days`-day rolling baseline.
    With no baseline yet (a brand-new tenant), today's spend can't be
    "anomalous" relative to nothing, so this reports `is_anomalous=False`
    rather than comparing against zero (which would flag every tenant's
    very first dollar spent). Logs a `wisetraceloom_spend_anomaly` warning
    event when anomalous — see module docstring for why this doesn't page
    anyone directly."""
    today_spend = get_daily_spend(tenant_id, as_of=as_of)
    baseline = get_rolling_baseline(tenant_id, as_of=as_of, window_days=window_days)

    is_anomalous = baseline is not None and baseline > 0 and today_spend > baseline * threshold_multiplier
    result = SpendAnomalyResult(
        is_anomalous=is_anomalous, today_spend=today_spend, baseline=baseline, threshold_multiplier=threshold_multiplier
    )
    if is_anomalous:
        get_logger("wisetraceloom.cost").warning(
            "wisetraceloom_spend_anomaly",
            tenant_id=tenant_id,
            today_spend_usd=today_spend,
            baseline_usd=baseline,
            threshold_multiplier=threshold_multiplier,
        )
    return result
