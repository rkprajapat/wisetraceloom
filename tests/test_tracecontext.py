import asyncio

import pytest

from trailwise.tracecontext import (
    bound_trace_context,
    current_span_id,
    current_trace_id,
    extract_traceparent,
    format_traceparent,
    generate_span_id,
    generate_trace_id,
    inject_traceparent,
    parse_traceparent,
)


def test_generated_ids_are_w3c_conformant_hex_lengths():
    trace_id = generate_trace_id()
    span_id = generate_span_id()
    assert len(trace_id) == 32
    assert len(span_id) == 16
    int(trace_id, 16)  # valid hex
    int(span_id, 16)


def test_format_and_parse_traceparent_round_trip():
    trace_id, span_id = generate_trace_id(), generate_span_id()
    header = format_traceparent(trace_id, span_id, sampled=True)
    assert header == f"00-{trace_id}-{span_id}-01"

    parsed = parse_traceparent(header)
    assert parsed == (trace_id, span_id, True)


def test_parse_traceparent_rejects_malformed_header():
    assert parse_traceparent("not-a-valid-header") is None
    assert parse_traceparent("00-tooshort-abc-01") is None


def test_parse_traceparent_rejects_all_zero_ids():
    assert parse_traceparent("00-" + "0" * 32 + "-" + "1" * 16 + "-01") is None
    assert parse_traceparent("00-" + "1" * 32 + "-" + "0" * 16 + "-01") is None


def test_bound_trace_context_sets_and_restores_current():
    assert current_trace_id() is None
    trace_id, span_id = generate_trace_id(), generate_span_id()
    with bound_trace_context(trace_id, span_id):
        assert current_trace_id() == trace_id
        assert current_span_id() == span_id
    assert current_trace_id() is None
    assert current_span_id() is None


def test_inject_traceparent_adds_header_from_current_context():
    trace_id, span_id = generate_trace_id(), generate_span_id()
    with bound_trace_context(trace_id, span_id):
        headers = inject_traceparent({"content-type": "application/json"})
    assert headers["traceparent"] == format_traceparent(trace_id, span_id)
    assert headers["content-type"] == "application/json"


def test_inject_traceparent_is_noop_copy_without_bound_context():
    headers = inject_traceparent({"x": "y"})
    assert headers == {"x": "y"}
    assert "traceparent" not in headers


def test_extract_traceparent_round_trips_through_headers_dict():
    trace_id, span_id = generate_trace_id(), generate_span_id()
    with bound_trace_context(trace_id, span_id):
        headers = inject_traceparent({})
    extracted = extract_traceparent(headers)
    assert extracted == (trace_id, span_id, True)


def test_extract_traceparent_returns_none_when_absent():
    assert extract_traceparent({}) is None


@pytest.mark.asyncio
async def test_trace_context_isolated_across_concurrent_asyncio_tasks():
    results = {}

    async def worker(name: str, delay: float) -> None:
        trace_id, span_id = generate_trace_id(), generate_span_id()
        with bound_trace_context(trace_id, span_id):
            await asyncio.sleep(delay)
            results[name] = (current_trace_id(), current_span_id())
            assert results[name] == (trace_id, span_id)

    await asyncio.gather(worker("a", 0.02), worker("b", 0.0))

    assert results["a"][0] != results["b"][0]


def test_nested_bound_trace_context_restores_outer_on_exit():
    outer_trace, outer_span = generate_trace_id(), generate_span_id()
    inner_trace, inner_span = generate_trace_id(), generate_span_id()

    with bound_trace_context(outer_trace, outer_span):
        with bound_trace_context(inner_trace, inner_span):
            assert current_trace_id() == inner_trace
            assert current_span_id() == inner_span
        assert current_trace_id() == outer_trace
        assert current_span_id() == outer_span
