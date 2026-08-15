import pytest

import wisetraceloom.config as config
from wisetraceloom.evaluation import (
    EvalCaseResult,
    EvalRegressionError,
    GoldenCase,
    clear_golden_set,
    get_eval_summary,
    set_golden_set,
    set_regression_thresholds,
)
from wisetraceloom.prompts import (
    register_prompt_version,
    resolve_prompt_alias,
    set_prompt_alias,
)

SLOT = "router_agent.system_prompt"
CASES = [GoldenCase(case_id=f"c{i}") for i in range(10)]


def _runner(*, n_pass: int = 10, cost: float = 1.0, latency: float = 100.0):
    def runner(template: str, case: GoldenCase) -> EvalCaseResult:
        idx = int(case.case_id[1:])
        return EvalCaseResult(passed=idx < n_pass, cost_usd=cost, latency_ms=latency)

    return runner


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_db_path_override", str(tmp_path / "test.db"))
    yield
    clear_golden_set()
    set_regression_thresholds()


def test_new_version_auto_runs_golden_set_eval():
    set_golden_set(SLOT, CASES, _runner(n_pass=9, cost=1.5, latency=80.0))
    version = register_prompt_version(SLOT, "v1 template")
    summary = get_eval_summary(version.id)
    assert summary is not None
    assert summary.n_cases == 10
    assert summary.n_passed == 9
    assert summary.pass_rate == pytest.approx(0.9)
    assert summary.mean_cost_usd == pytest.approx(1.5)
    assert summary.p95_latency_ms == pytest.approx(80.0)
    assert summary.slot_name == SLOT


def test_repeat_hash_does_not_re_run_eval():
    calls: list[str] = []

    def runner(template: str, case: GoldenCase) -> EvalCaseResult:
        calls.append(case.case_id)
        return EvalCaseResult(passed=True)

    set_golden_set(SLOT, [GoldenCase("a")], runner)
    first = register_prompt_version(SLOT, "same template")
    second = register_prompt_version(SLOT, "same template")
    assert first.id == second.id
    assert calls == ["a"]


def test_no_golden_set_means_no_eval_and_production_stays_ungated():
    v1 = register_prompt_version(SLOT, "v1")
    v2 = register_prompt_version(SLOT, "v2")
    assert get_eval_summary(v1.id) is None
    set_prompt_alias(SLOT, "production", v1.id)
    set_prompt_alias(SLOT, "production", v2.id)
    assert resolve_prompt_alias(SLOT, "production").id == v2.id


def test_first_production_promotion_allowed_with_eval_and_no_baseline():
    set_golden_set(SLOT, CASES, _runner())
    v1 = register_prompt_version(SLOT, "v1")
    set_prompt_alias(SLOT, "production", v1.id)
    assert resolve_prompt_alias(SLOT, "production").id == v1.id


def test_production_blocked_when_pass_rate_drops_more_than_two_percent():
    set_golden_set(SLOT, CASES, _runner(n_pass=10))
    v1 = register_prompt_version(SLOT, "v1")
    set_prompt_alias(SLOT, "production", v1.id)
    set_golden_set(SLOT, CASES, _runner(n_pass=9))
    v2 = register_prompt_version(SLOT, "v2")
    with pytest.raises(EvalRegressionError, match="pass rate"):
        set_prompt_alias(SLOT, "production", v2.id)
    assert resolve_prompt_alias(SLOT, "production").id == v1.id


def test_production_blocked_when_cost_increases_more_than_fifteen_percent():
    set_golden_set(SLOT, CASES, _runner(cost=1.0))
    v1 = register_prompt_version(SLOT, "v1")
    set_prompt_alias(SLOT, "production", v1.id)
    set_golden_set(SLOT, CASES, _runner(cost=1.16))
    v2 = register_prompt_version(SLOT, "v2")
    with pytest.raises(EvalRegressionError, match="cost"):
        set_prompt_alias(SLOT, "production", v2.id)


def test_production_blocked_when_p95_latency_increases_more_than_twenty_percent():
    set_golden_set(SLOT, CASES, _runner(latency=100.0))
    v1 = register_prompt_version(SLOT, "v1")
    set_prompt_alias(SLOT, "production", v1.id)
    set_golden_set(SLOT, CASES, _runner(latency=121.0))
    v2 = register_prompt_version(SLOT, "v2")
    with pytest.raises(EvalRegressionError, match="p95 latency"):
        set_prompt_alias(SLOT, "production", v2.id)


def test_production_allowed_when_metrics_stay_within_thresholds():
    set_golden_set(SLOT, CASES, _runner(n_pass=10, cost=1.0, latency=100.0))
    v1 = register_prompt_version(SLOT, "v1")
    set_prompt_alias(SLOT, "production", v1.id)
    # 0% pass-rate drop, +14% cost, +19% p95 — all under 2/15/20.
    set_golden_set(SLOT, CASES, _runner(n_pass=10, cost=1.14, latency=119.0))
    v2 = register_prompt_version(SLOT, "v2")
    set_prompt_alias(SLOT, "production", v2.id)
    assert resolve_prompt_alias(SLOT, "production").id == v2.id


def test_canary_and_shadow_are_not_gated():
    set_golden_set(SLOT, CASES, _runner(n_pass=10))
    v1 = register_prompt_version(SLOT, "v1")
    set_prompt_alias(SLOT, "production", v1.id)
    set_golden_set(SLOT, CASES, _runner(n_pass=0, cost=9.0, latency=999.0))
    v2 = register_prompt_version(SLOT, "v2")
    set_prompt_alias(SLOT, "canary", v2.id)
    set_prompt_alias(SLOT, "shadow", v2.id)
    assert resolve_prompt_alias(SLOT, "canary").id == v2.id
    assert resolve_prompt_alias(SLOT, "shadow").id == v2.id
    with pytest.raises(EvalRegressionError):
        set_prompt_alias(SLOT, "production", v2.id)


def test_production_blocked_when_golden_set_configured_but_eval_failed():
    def boom(template: str, case: GoldenCase) -> EvalCaseResult:
        raise RuntimeError("judge down")

    set_golden_set(SLOT, CASES, boom)
    v1 = register_prompt_version(SLOT, "v1")
    assert v1.id is not None
    assert get_eval_summary(v1.id) is None
    with pytest.raises(EvalRegressionError, match="no golden-set eval"):
        set_prompt_alias(SLOT, "production", v1.id)


def test_empty_golden_set_rejected():
    with pytest.raises(EvalRegressionError, match="at least one case"):
        set_golden_set(SLOT, [], _runner())
    with pytest.raises(EvalRegressionError, match="slot_name must be non-empty"):
        set_golden_set("  ", CASES, _runner())


def test_thresholds_are_configurable():
    set_golden_set(SLOT, CASES, _runner(n_pass=10))
    v1 = register_prompt_version(SLOT, "v1")
    set_prompt_alias(SLOT, "production", v1.id)
    set_golden_set(SLOT, CASES, _runner(n_pass=9))
    v2 = register_prompt_version(SLOT, "v2")
    set_regression_thresholds(pass_rate_max_drop=0.5)
    set_prompt_alias(SLOT, "production", v2.id)
    assert resolve_prompt_alias(SLOT, "production").id == v2.id
