import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

import trailwise.config as config
from trailwise.otel_export import (
    export_agent_span,
    export_llm_span,
    export_tool_span,
    gen_ai_semconv_enabled,
    set_export_config,
)
from trailwise.schema import AgentSpan, LLMSpan, ToolSpan


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_db_path_override", str(tmp_path / "cfg.db"))


@pytest.fixture
def span_exporter():
    return InMemorySpanExporter()


@pytest.fixture
def tracer_provider(span_exporter):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    return provider


@pytest.fixture
def metric_reader():
    return InMemoryMetricReader()


@pytest.fixture
def meter_provider(metric_reader):
    return MeterProvider(metric_readers=[metric_reader])


@pytest.fixture(autouse=True)
def _opt_in():
    set_export_config(gen_ai_semconv_enabled=True)


def test_opt_in_is_per_tenant_with_global_fallback():
    set_export_config(tenant_id="acme", gen_ai_semconv_enabled=False)
    assert gen_ai_semconv_enabled(tenant_id="acme") is False
    assert gen_ai_semconv_enabled(tenant_id="other-tenant") is True


def test_export_is_a_noop_when_not_opted_in(tracer_provider, span_exporter):
    set_export_config(gen_ai_semconv_enabled=False)
    span = AgentSpan(
        span_id="s1", trace_id="t1", agent_id="a1", agent_name="router_agent",
        operation_name="invoke_agent",
    )
    export_agent_span(span, tracer_provider=tracer_provider)
    assert span_exporter.get_finished_spans() == ()


def test_export_agent_span_carries_gen_ai_agent_attributes(tracer_provider, span_exporter):
    span = AgentSpan(
        span_id="s1",
        trace_id="t1",
        agent_id="router-1",
        agent_name="router_agent",
        operation_name="invoke_agent",
        loop_iteration=2,
        tenant_id="acme",
    )
    export_agent_span(span, tracer_provider=tracer_provider)

    [exported] = span_exporter.get_finished_spans()
    assert exported.name == "invoke_agent router_agent"
    assert exported.attributes["gen_ai.agent.id"] == "router-1"
    assert exported.attributes["gen_ai.agent.name"] == "router_agent"
    assert exported.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert exported.attributes["trailwise.loop_iteration"] == 2
    assert exported.attributes["trailwise.trace_id"] == "t1"
    assert exported.attributes["trailwise.tenant_id"] == "acme"


def test_export_tool_span_sets_error_status_on_failure(tracer_provider, span_exporter):
    span = ToolSpan(
        span_id="s2", trace_id="t1", tool_name="search",
        success=False, error_message="timeout",
    )
    export_tool_span(span, tracer_provider=tracer_provider)

    [exported] = span_exporter.get_finished_spans()
    assert exported.name == "execute_tool search"
    assert exported.attributes["gen_ai.tool.name"] == "search"
    assert exported.status.status_code == StatusCode.ERROR
    assert exported.status.description == "timeout"


def test_export_llm_span_carries_usage_attributes_and_records_metrics(tracer_provider, span_exporter, meter_provider, metric_reader):
    span = LLMSpan(
        span_id="s3",
        trace_id="t1",
        provider_name="anthropic",
        request_model="claude-sonnet-5",
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=10,
        estimated_cost_usd=0.0042,
        prompt_version_id="router_agent.system_prompt-v1",
    )
    export_llm_span(span, tracer_provider=tracer_provider, meter_provider=meter_provider)

    [exported] = span_exporter.get_finished_spans()
    assert exported.name == "chat claude-sonnet-5"
    assert exported.attributes["gen_ai.provider.name"] == "anthropic"
    assert exported.attributes["gen_ai.request.model"] == "claude-sonnet-5"
    assert exported.attributes["gen_ai.usage.input_tokens"] == 100
    assert exported.attributes["gen_ai.usage.output_tokens"] == 50
    assert exported.attributes["gen_ai.usage.cache_read.input_tokens"] == 10
    assert exported.attributes["trailwise.estimated_cost_usd"] == 0.0042
    assert exported.attributes["trailwise.prompt_version_id"] == "router_agent.system_prompt-v1"

    metrics_data = metric_reader.get_metrics_data()
    metric_names = {
        metric.name
        for rm in metrics_data.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
    }
    assert "gen_ai.client.token.usage" in metric_names
    assert "gen_ai.client.operation.duration" in metric_names
