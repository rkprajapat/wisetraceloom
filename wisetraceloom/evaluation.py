"""Automated eval-on-detection + production-promotion regression gate (PRD §8.3 — feature 2.8).

When a new prompt version is auto-detected (feature 1.6's
`register_prompt_version`), this module optionally runs a host-supplied
golden/regression suite against it and persists the aggregate
(pass rate, mean cost, p95 latency). Promotion to the `production` alias
(feature 2.7) is then blocked if those aggregates regress past configured
thresholds relative to the version `production` currently points at.

The SDK does not call an LLM. The host registers an `EvalRunner` callable
— the same caller-supplied-callable precedent as feature 2.3's `AnchorSink`
and feature 2.6's `MaskingCallback` — because only the host knows how to
score a case (string match, tool-selection check, LLM-as-judge, …). No
golden set registered = eval is a no-op and `production` promotion stays
unrestricted, matching PRD §8.3's "optionally trigger." Once a suite *is*
registered for a slot, a candidate without a stored summary is not
eligible for `production` (fail-closed governance, not feature 1.5's
instrumentation fail-open). `canary`/`shadow` are never gated.

Default thresholds match PRD §8.3's examples: pass-rate drop >2%, cost
increase >15%, p95-latency increase >20%. Cost/latency gates skip when
the baseline's value is 0 — same "unknown ≠ free" distinction as feature
2.4's `estimate_cost_usd` returning `None` rather than `$0`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlmodel import Field, Session, SQLModel, select

from wisetraceloom.config import get_engine

# Runner sees the new version's template so it can actually exercise it;
# `model_params` are closed over by the host if needed (YAGNI here).
EvalRunner = Callable[[str, "GoldenCase"], "EvalCaseResult"]


class EvalRegressionError(Exception):
    """Production promotion blocked: missing eval, or metrics regressed
    past the configured thresholds against the current `production` version."""


@dataclass(frozen=True)
class GoldenCase:
    """One row of a slot's golden/regression set. `input`/`expected` are
    opaque to this SDK — the host runner interprets them."""

    case_id: str
    input: Any = None
    expected: Any = None


@dataclass(frozen=True)
class EvalCaseResult:
    """Per-case outcome the host runner returns. `cost_usd`/`latency_ms`
    default to 0 so a correctness-only runner still produces a summary;
    a 0 baseline then skips that metric's regression gate (see module
    docstring)."""

    passed: bool
    cost_usd: float = 0.0
    latency_ms: float = 0.0


class EvalSummary(SQLModel, table=True):
    """Persisted aggregate of one golden-set run against one prompt version.

    Unique on `prompt_version_id` — re-running eval overwrites, it does not
    append. Promotion gating reads this row, not the live runner, so a later
    process can gate without the original callable still being registered.
    """

    id: int | None = Field(default=None, primary_key=True)
    prompt_version_id: int = Field(index=True, unique=True)
    slot_name: str = Field(index=True)
    n_cases: int
    n_passed: int
    pass_rate: float
    mean_cost_usd: float
    p95_latency_ms: float
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class RegressionThresholds:
    """Relative-change caps that block `production` promotion. Deltas are
    fractions (0.02 = 2%), not percentage points vs 100."""

    pass_rate_max_drop: float = 0.02
    cost_max_increase: float = 0.15
    p95_latency_max_increase: float = 0.20


@dataclass(frozen=True)
class _GoldenSuite:
    cases: tuple[GoldenCase, ...]
    runner: EvalRunner


_suites: dict[str, _GoldenSuite] = {}
_thresholds = RegressionThresholds()


def set_golden_set(slot_name: str, cases: list[GoldenCase], runner: EvalRunner) -> None:
    """Register the golden/regression suite auto-run on new versions of
    `slot_name`. Process-local (the runner is a callable — cannot live in
    SQLite), same as `set_masking_callback`. Replaces any prior suite for
    the slot."""
    slot_name = slot_name.strip()
    if not slot_name:
        raise EvalRegressionError("slot_name must be non-empty")
    if not cases:
        raise EvalRegressionError("golden set must contain at least one case")
    _suites[slot_name] = _GoldenSuite(cases=tuple(cases), runner=runner)


def clear_golden_set(slot_name: str | None = None) -> None:
    """Drop one slot's suite, or every suite when `slot_name` is `None`.
    Idempotent. Used by tests to keep process-local state from leaking."""
    if slot_name is None:
        _suites.clear()
        return
    _suites.pop(slot_name, None)


def set_regression_thresholds(
    *,
    pass_rate_max_drop: float = 0.02,
    cost_max_increase: float = 0.15,
    p95_latency_max_increase: float = 0.20,
) -> None:
    """Override the PRD §8.3 default caps. Process-local, like the golden
    set — a policy the host sets in-process, not an env var."""
    _thresholds.pass_rate_max_drop = pass_rate_max_drop
    _thresholds.cost_max_increase = cost_max_increase
    _thresholds.p95_latency_max_increase = p95_latency_max_increase


def get_eval_summary(version_id: int) -> EvalSummary | None:
    """The stored golden-set aggregate for `version_id`, or `None` if that
    version has never been eval'd."""
    with Session(get_engine()) as session:
        return session.exec(
            select(EvalSummary).where(EvalSummary.prompt_version_id == version_id)
        ).first()


def _p95(values: list[float]) -> float:
    """Nearest-rank 95th percentile. One value → that value; empty → 0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[min(idx, len(ordered) - 1)]


def _persist_summary(summary: EvalSummary) -> EvalSummary:
    with Session(get_engine()) as session:
        existing = session.exec(
            select(EvalSummary).where(EvalSummary.prompt_version_id == summary.prompt_version_id)
        ).first()
        if existing is None:
            session.add(summary)
            session.commit()
            session.refresh(summary)
            return summary
        existing.n_cases = summary.n_cases
        existing.n_passed = summary.n_passed
        existing.pass_rate = summary.pass_rate
        existing.mean_cost_usd = summary.mean_cost_usd
        existing.p95_latency_ms = summary.p95_latency_ms
        existing.evaluated_at = summary.evaluated_at
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing


def run_golden_eval(version_id: int, template: str) -> EvalSummary:
    """Run the slot's registered suite against `template` and persist the
    aggregate. Raises `EvalRegressionError` if no suite is registered —
    explicit eval is not optional the way auto-on-detection is."""
    from wisetraceloom.prompts import get_prompt_version

    version = get_prompt_version(version_id)
    suite = _suites.get(version.slot_name)
    if suite is None:
        raise EvalRegressionError(f"no golden set registered for slot {version.slot_name!r}")

    results = [suite.runner(template, case) for case in suite.cases]
    n_cases = len(results)
    n_passed = sum(1 for r in results if r.passed)
    summary = EvalSummary(
        prompt_version_id=version_id,
        slot_name=version.slot_name,
        n_cases=n_cases,
        n_passed=n_passed,
        pass_rate=n_passed / n_cases,
        mean_cost_usd=sum(r.cost_usd for r in results) / n_cases,
        p95_latency_ms=_p95([r.latency_ms for r in results]),
    )
    return _persist_summary(summary)


def maybe_eval_on_detection(version: Any, template: str) -> EvalSummary | None:
    """Called by `register_prompt_version` on a newly minted version.
    No-op when the slot has no golden set. A runner exception is swallowed
    (detection still succeeded) and logged — production promotion then
    fails closed for lack of a summary. `version` is a `PromptVersion`;
    typed as `Any` to avoid a module-level import cycle with `prompts.py`."""
    if version.slot_name not in _suites:
        return None
    try:
        return run_golden_eval(version.id, template)
    except Exception as exc:
        try:
            from wisetraceloom.logging import get_logger

            get_logger("wisetraceloom.evaluation").warning(
                "wisetraceloom_eval_on_detection_failed",
                slot_name=version.slot_name,
                prompt_version_id=version.id,
                exc_type=type(exc).__name__,
                exc_message=str(exc),
            )
        except Exception:
            pass
        return None


def assert_production_promotion_allowed(slot_name: str, version_id: int) -> None:
    """Fail-closed gate used by `set_prompt_alias` when the alias is
    `production`. No golden set for the slot → no-op (eval is optional).
    Golden set configured but candidate has no summary → blocked. No
    current `production` pointer, or that pointer has no summary → allowed
    (nothing to regress against, same as feature 2.4's first-dollar
    anomaly rule). Re-pointing `production` at itself → allowed."""
    if slot_name not in _suites:
        return

    candidate = get_eval_summary(version_id)
    if candidate is None:
        raise EvalRegressionError(
            f"version {version_id} has no golden-set eval; production promotion is blocked"
        )

    from wisetraceloom.prompts import resolve_prompt_alias

    current = resolve_prompt_alias(slot_name, "production")
    if current is None or current.id == version_id:
        return

    baseline = get_eval_summary(current.id)
    if baseline is None:
        return

    failures: list[str] = []
    drop = baseline.pass_rate - candidate.pass_rate
    if drop > _thresholds.pass_rate_max_drop:
        failures.append(
            f"pass rate {candidate.pass_rate:.4f} dropped {drop:.4f} from "
            f"{baseline.pass_rate:.4f} (max drop {_thresholds.pass_rate_max_drop:.4f})"
        )
    if baseline.mean_cost_usd > 0:
        cost_increase = (candidate.mean_cost_usd - baseline.mean_cost_usd) / baseline.mean_cost_usd
        if cost_increase > _thresholds.cost_max_increase:
            failures.append(
                f"cost {candidate.mean_cost_usd:.4f} increased {cost_increase:.4f} from "
                f"{baseline.mean_cost_usd:.4f} (max increase {_thresholds.cost_max_increase:.4f})"
            )
    if baseline.p95_latency_ms > 0:
        latency_increase = (candidate.p95_latency_ms - baseline.p95_latency_ms) / baseline.p95_latency_ms
        if latency_increase > _thresholds.p95_latency_max_increase:
            failures.append(
                f"p95 latency {candidate.p95_latency_ms:.4f} increased {latency_increase:.4f} from "
                f"{baseline.p95_latency_ms:.4f} (max increase {_thresholds.p95_latency_max_increase:.4f})"
            )
    if failures:
        raise EvalRegressionError(
            f"version {version_id} failed production regression gate vs version {current.id}: "
            + "; ".join(failures)
        )
