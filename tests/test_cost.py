from datetime import date, timedelta

import pytest

import wisetraceloom.config as config
from wisetraceloom.cost import (
    QuotaExceededError,
    assert_within_quota,
    check_spend_anomaly,
    estimate_cost_usd,
    get_daily_spend,
    get_rolling_baseline,
    record_spend,
    set_pricing_config,
    set_quota_config,
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_db_path_override", str(tmp_path / "test.db"))


@pytest.fixture(autouse=True)
def _configured():
    from wisetraceloom.logging import configure

    configure()


TODAY = date(2026, 7, 31)


def test_estimate_cost_usd_returns_none_without_pricing_config():
    assert estimate_cost_usd("anthropic", "claude-sonnet-5", input_tokens=1000, output_tokens=1000) is None


def test_estimate_cost_usd_computes_from_pricing_config():
    set_pricing_config("anthropic", "claude-sonnet-5", input_cost_per_1k_usd=3.0, output_cost_per_1k_usd=15.0)

    cost = estimate_cost_usd("anthropic", "claude-sonnet-5", input_tokens=1000, output_tokens=1000)

    assert cost == pytest.approx(3.0 + 15.0)


def test_estimate_cost_usd_includes_cache_layers():
    set_pricing_config(
        "anthropic",
        "claude-sonnet-5",
        input_cost_per_1k_usd=3.0,
        output_cost_per_1k_usd=15.0,
        cache_read_cost_per_1k_usd=0.3,
        cache_creation_cost_per_1k_usd=3.75,
    )

    cost = estimate_cost_usd(
        "anthropic",
        "claude-sonnet-5",
        input_tokens=1000,
        output_tokens=1000,
        cache_read_input_tokens=1000,
        cache_creation_input_tokens=1000,
    )

    assert cost == pytest.approx(3.0 + 15.0 + 0.3 + 3.75)


def test_tenant_pricing_config_overrides_global_default():
    set_pricing_config("anthropic", "claude-sonnet-5", input_cost_per_1k_usd=3.0, output_cost_per_1k_usd=15.0)
    set_pricing_config(
        "anthropic", "claude-sonnet-5", tenant_id="acme", input_cost_per_1k_usd=1.0, output_cost_per_1k_usd=1.0
    )

    acme_cost = estimate_cost_usd(
        "anthropic", "claude-sonnet-5", tenant_id="acme", input_tokens=1000, output_tokens=1000
    )
    other_cost = estimate_cost_usd(
        "anthropic", "claude-sonnet-5", tenant_id="other-tenant", input_tokens=1000, output_tokens=1000
    )

    assert acme_cost == pytest.approx(2.0)
    assert other_cost == pytest.approx(18.0)


def test_record_spend_accumulates_same_day():
    record_spend("acme", 1.5, as_of=TODAY)
    record_spend("acme", 2.5, as_of=TODAY)

    assert get_daily_spend("acme", as_of=TODAY) == pytest.approx(4.0)


def test_record_spend_is_isolated_per_tenant_and_day():
    record_spend("acme", 5.0, as_of=TODAY)
    record_spend("acme", 5.0, as_of=TODAY - timedelta(days=1))
    record_spend("other-tenant", 5.0, as_of=TODAY)

    assert get_daily_spend("acme", as_of=TODAY) == pytest.approx(5.0)
    assert get_daily_spend("acme", as_of=TODAY - timedelta(days=1)) == pytest.approx(5.0)
    assert get_daily_spend("other-tenant", as_of=TODAY) == pytest.approx(5.0)


def test_record_spend_concurrent_callers_no_lost_updates():
    from concurrent.futures import ThreadPoolExecutor

    n = 50
    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(lambda i: record_spend("acme", 1.0, as_of=TODAY), range(n)))

    assert get_daily_spend("acme", as_of=TODAY) == pytest.approx(50.0)


def test_get_daily_spend_zero_when_nothing_recorded():
    assert get_daily_spend("nobody", as_of=TODAY) == 0.0


def test_assert_within_quota_noop_without_quota_config():
    record_spend("acme", 1000.0, as_of=TODAY)
    assert_within_quota("acme", as_of=TODAY)  # no cap configured -> never trips


def test_assert_within_quota_passes_under_cap():
    set_quota_config(tenant_id="acme", daily_cap_usd=10.0)
    record_spend("acme", 5.0, as_of=TODAY)
    assert_within_quota("acme", as_of=TODAY)


def test_assert_within_quota_raises_at_or_over_cap():
    set_quota_config(tenant_id="acme", daily_cap_usd=10.0)
    record_spend("acme", 10.0, as_of=TODAY)

    with pytest.raises(QuotaExceededError):
        assert_within_quota("acme", as_of=TODAY)


def test_assert_within_quota_tenant_config_overrides_global():
    set_quota_config(daily_cap_usd=10.0)
    set_quota_config(tenant_id="acme", daily_cap_usd=1000.0)
    record_spend("acme", 500.0, as_of=TODAY)

    assert_within_quota("acme", as_of=TODAY)  # under acme's own (higher) cap


def test_get_rolling_baseline_none_without_history():
    assert get_rolling_baseline("acme", as_of=TODAY) is None


def test_get_rolling_baseline_averages_over_window_excluding_today():
    for offset in range(1, 8):
        record_spend("acme", 7.0, as_of=TODAY - timedelta(days=offset))
    record_spend("acme", 1000.0, as_of=TODAY)  # today itself must not count

    baseline = get_rolling_baseline("acme", as_of=TODAY, window_days=7)

    assert baseline == pytest.approx(7.0)


def test_check_spend_anomaly_false_without_baseline():
    record_spend("acme", 1000.0, as_of=TODAY)
    result = check_spend_anomaly("acme", as_of=TODAY)
    assert result.is_anomalous is False


def test_check_spend_anomaly_false_within_threshold():
    for offset in range(1, 8):
        record_spend("acme", 10.0, as_of=TODAY - timedelta(days=offset))
    record_spend("acme", 15.0, as_of=TODAY)

    result = check_spend_anomaly("acme", as_of=TODAY, threshold_multiplier=2.0)

    assert result.is_anomalous is False
    assert result.baseline == pytest.approx(10.0)


def test_check_spend_anomaly_true_over_threshold():
    for offset in range(1, 8):
        record_spend("acme", 10.0, as_of=TODAY - timedelta(days=offset))
    record_spend("acme", 25.0, as_of=TODAY)

    result = check_spend_anomaly("acme", as_of=TODAY, threshold_multiplier=2.0)

    assert result.is_anomalous is True
    assert result.today_spend == pytest.approx(25.0)


def test_llm_call_auto_attributes_cost_and_records_tenant_spend():
    from wisetraceloom.instrumentation import llm_call

    set_pricing_config("anthropic", "claude-sonnet-5", input_cost_per_1k_usd=3.0, output_cost_per_1k_usd=15.0)

    with llm_call("anthropic", "claude-sonnet-5", tenant_id="acme") as span:
        span.input_tokens = 1000
        span.output_tokens = 1000

    assert span.estimated_cost_usd == pytest.approx(18.0)
    assert get_daily_spend("acme") == pytest.approx(18.0)


def test_llm_call_does_not_override_host_supplied_cost():
    from wisetraceloom.instrumentation import llm_call

    set_pricing_config("anthropic", "claude-sonnet-5", input_cost_per_1k_usd=3.0, output_cost_per_1k_usd=15.0)

    with llm_call("anthropic", "claude-sonnet-5", tenant_id="acme") as span:
        span.input_tokens = 1000
        span.output_tokens = 1000
        span.estimated_cost_usd = 0.5  # host already knows the real cost

    assert span.estimated_cost_usd == 0.5
    assert get_daily_spend("acme") == pytest.approx(0.5)


def test_llm_call_without_pricing_config_leaves_cost_unset_and_records_nothing():
    from wisetraceloom.instrumentation import llm_call

    with llm_call("anthropic", "claude-sonnet-5", tenant_id="acme") as span:
        span.input_tokens = 1000
        span.output_tokens = 1000

    assert span.estimated_cost_usd is None
    assert get_daily_spend("acme") == 0.0
