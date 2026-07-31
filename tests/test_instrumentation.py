import pytest
import structlog

import trailwise.config as config
from trailwise.instrumentation import agent_step, llm_call, tool_call, trace_tool_call
from trailwise.logging import configure
from trailwise.otel_export import set_export_config
from trailwise.tracecontext import current_span_id, current_trace_id

_CAPTURE_PROCESSORS = (structlog.contextvars.merge_contextvars,)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_db_path_override", str(tmp_path / "cfg.db"))


@pytest.fixture(autouse=True)
def _configured():
    configure()


def test_agent_step_yields_span_with_w3c_ids_and_stamps_ended_at():
    with agent_step("a1", "router_agent") as span:
        assert len(span.trace_id) == 32
        assert len(span.span_id) == 16
        assert span.ended_at is None
    assert span.ended_at is not None


def test_tool_call_marks_success_true_on_clean_exit():
    with tool_call("search") as span:
        pass
    assert span.success is True


def test_tool_call_marks_success_false_and_reraises_on_exception():
    with pytest.raises(ValueError):
        with tool_call("search") as span:
            raise ValueError("tool blew up")
    assert span.success is False
    assert span.error_message == "tool blew up"


def test_llm_call_lets_caller_set_usage_fields_inside_block():
    with llm_call("anthropic", "claude-sonnet-5") as span:
        span.input_tokens = 100
        span.output_tokens = 42
    assert span.input_tokens == 100
    assert span.output_tokens == 42


def test_nested_tool_call_inside_agent_step_shares_trace_and_parents_correctly():
    with agent_step("a1", "router_agent") as agent_span:
        with tool_call("search") as tool_span:
            pass
    assert tool_span.trace_id == agent_span.trace_id
    assert tool_span.parent_span_id == agent_span.span_id
    assert agent_span.parent_span_id is None


def test_current_trace_context_restored_after_block_exits():
    assert current_trace_id() is None
    with agent_step("a1", "router_agent"):
        assert current_trace_id() is not None
    assert current_trace_id() is None
    assert current_span_id() is None


def test_agent_step_emits_structured_log_event():
    with structlog.testing.capture_logs(processors=_CAPTURE_PROCESSORS) as captured:
        with agent_step("a1", "router_agent", tenant_id="acme"):
            pass

    agent_events = [e for e in captured if e["event"] == "agent_span"]
    assert len(agent_events) == 1
    assert agent_events[0]["agent_id"] == "a1"
    assert agent_events[0]["tenant_id"] == "acme"


def test_span_exported_to_otel_when_opted_in(tmp_path):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    import trailwise.instrumentation as instrumentation

    set_export_config(gen_ai_semconv_enabled=True)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    original = instrumentation.export_tool_span
    instrumentation.export_tool_span = lambda span: original(span, tracer_provider=provider)
    try:
        with tool_call("search", tenant_id="acme"):
            pass
    finally:
        instrumentation.export_tool_span = original

    [exported] = exporter.get_finished_spans()
    assert exported.attributes["gen_ai.tool.name"] == "search"


def test_decorator_sugar_wraps_whole_function_as_one_step():
    @trace_tool_call("search")
    def do_search(query: str) -> str:
        assert current_trace_id() is not None
        return f"results for {query}"

    assert current_trace_id() is None
    result = do_search("cats")
    assert result == "results for cats"
    assert current_trace_id() is None


def test_emit_failure_does_not_propagate_to_caller(monkeypatch):
    import trailwise.instrumentation as instrumentation

    def broken_export(span):
        raise RuntimeError("exporter down")

    monkeypatch.setattr(instrumentation, "export_tool_span", broken_export)

    with tool_call("search") as span:
        pass  # no exception should escape despite the broken exporter

    assert span.success is True
