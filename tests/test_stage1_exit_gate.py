"""Stage 1 exit gate (feature 1.9): <5% latency overhead, zero exceptions
propagated to the host under fault injection. PRD Recommendations Stage 1
threshold: "SDK adds <5% latency overhead and never propagates exceptions
to the host application."
"""

import contextlib
import io
import statistics
import time

import pytest

import trailwise.config as config
import trailwise.instrumentation as instrumentation
from trailwise.instrumentation import agent_step, llm_call, tool_call
from trailwise.logging import configure

LATENCY_OVERHEAD_THRESHOLD = 0.05  # PRD Stage 1 exit gate: <5%
BASELINE_CALL_SECONDS = 0.05  # a fast simulated LLM/tool call
ITERATIONS = 40


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_db_path_override", str(tmp_path / "cfg.db"))


@pytest.fixture(autouse=True)
def _configured():
    configure()


def _median_duration(fn, iterations: int) -> float:
    # Log output is noise for a timing benchmark, not a correctness signal
    # here — silenced so it doesn't skew wall-clock measurement via stdout
    # flushing overhead.
    durations = []
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(iterations):
            start = time.perf_counter()
            fn()
            durations.append(time.perf_counter() - start)
    return statistics.median(durations)


def test_instrumentation_overhead_is_under_five_percent():
    def baseline():
        time.sleep(BASELINE_CALL_SECONDS)

    def instrumented():
        with tool_call("search") as span:
            time.sleep(BASELINE_CALL_SECONDS)
            span.success = True

    baseline()
    instrumented()  # warm up (lazy engine/model construction)

    baseline_median = _median_duration(baseline, ITERATIONS)
    instrumented_median = _median_duration(instrumented, ITERATIONS)

    overhead_ratio = (instrumented_median - baseline_median) / baseline_median
    assert overhead_ratio < LATENCY_OVERHEAD_THRESHOLD, (
        f"instrumentation overhead {overhead_ratio:.1%} exceeds the "
        f"{LATENCY_OVERHEAD_THRESHOLD:.0%} Stage 1 exit-gate threshold "
        f"(baseline={baseline_median * 1000:.2f}ms, "
        f"instrumented={instrumented_median * 1000:.2f}ms)"
    )


@pytest.mark.parametrize(
    "context_manager_factory,export_attr",
    [
        (lambda: agent_step("a1", "router_agent"), "export_agent_span"),
        (lambda: tool_call("search"), "export_tool_span"),
        (lambda: llm_call("anthropic", "claude-sonnet-5"), "export_llm_span"),
    ],
)
def test_chaos_broken_export_never_propagates(monkeypatch, context_manager_factory, export_attr):
    def broken_export(span, *args, **kwargs):
        raise RuntimeError(f"{export_attr} exploded")

    monkeypatch.setattr(instrumentation, export_attr, broken_export)

    with context_manager_factory():
        pass  # must not raise despite the exporter always failing


def test_chaos_broken_structlog_logger_never_propagates(monkeypatch):
    def broken_get_logger(*args, **kwargs):
        raise RuntimeError("logging pipeline down")

    monkeypatch.setattr(instrumentation, "get_logger", broken_get_logger)

    with tool_call("search"):
        pass  # must not raise despite the logger itself being unusable


def test_chaos_broken_logger_and_exporter_simultaneously_never_propagates(monkeypatch):
    def broken_get_logger(*args, **kwargs):
        raise RuntimeError("logging pipeline down")

    def broken_export(span, *args, **kwargs):
        raise RuntimeError("exporter down")

    monkeypatch.setattr(instrumentation, "get_logger", broken_get_logger)
    monkeypatch.setattr(instrumentation, "export_tool_span", broken_export)

    with agent_step("a1", "router_agent"):
        with tool_call("search"):
            pass  # neither failure should escape either context manager


def test_host_business_logic_exceptions_still_propagate_through_instrumentation():
    # Fail-open protects trailwise's own instrumentation, never the host's
    # business logic — this must keep working even under the chaos above.
    with pytest.raises(ValueError, match="real bug"):
        with tool_call("search"):
            raise ValueError("real bug")

    with pytest.raises(ValueError, match="real bug"):
        with llm_call("anthropic", "claude-sonnet-5"):
            raise ValueError("real bug")

    with pytest.raises(ValueError, match="real bug"):
        with agent_step("a1", "router_agent"):
            raise ValueError("real bug")


def test_host_exception_still_propagates_even_with_broken_exporter(monkeypatch):
    # The two failure modes are independent: a broken exporter must not
    # mask a real host bug, and a real host bug must not stop the (fail-open)
    # exporter failure from also being swallowed correctly.
    def broken_export(span, *args, **kwargs):
        raise RuntimeError("exporter down")

    monkeypatch.setattr(instrumentation, "export_tool_span", broken_export)

    with pytest.raises(ValueError, match="real bug"):
        with tool_call("search"):
            raise ValueError("real bug")
