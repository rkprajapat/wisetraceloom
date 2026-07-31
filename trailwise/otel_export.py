"""OTel `gen_ai.*` export adapter (PRD §1, §2, Stage 1 recommendation (c)).

Maps `trailwise.schema`'s internal rich spans (`AgentSpan`, `ToolSpan`,
`LLMSpan`) onto OTel spans/metrics using the `gen_ai.*` attribute names from
`open-telemetry/semantic-conventions-genai`. This is the "OTel wire format
for export" half of the hybrid architecture (PRD §2) — the internal schema
stays the source of truth; this module only translates it outward.

**Stability opt-in.** `gen_ai.*` remains Development-status (PRD caveats,
§1). Per OTel's stability opt-in pattern, export is a no-op until a host
explicitly opts in via `set_export_config(gen_ai_semconv_enabled=True)` —
mirroring the effect of OTel's own `OTEL_SEMCONV_STABILITY_OPT_IN` env var,
but as a persisted, per-tenant-overridable `ExportConfig` row (SQLModel,
same store as `trailwise.config`) rather than an environment variable, so
every knob in this SDK is set the same explicit, code-driven way. There is
no legacy (pre-v1.37) schema to fall back to here — "not opted in" means
"don't export `gen_ai.*` at all" until the host asks for it.

**ID correlation, not ID identity.** `AgentSpan`/`ToolSpan`/`LLMSpan` carry
plain `trace_id`/`span_id` strings, not W3C-conformant 128/64-bit ids (that
lands with feature 1.8's trace-context propagation). Rather than fake byte-
level precision by hashing our ids into OTel's id space, exported spans use
OTel's own generated trace/span ids and carry the original `trailwise.*`
ids as plain attributes for correlation. This also means exported spans
aren't nested under each other via `parent_span_id` yet — each export call
produces a standalone OTel span; the `trailwise.parent_span_id` attribute
preserves the logical link until 1.7/1.8 land and can wire real context
propagation through.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from opentelemetry import metrics, trace
from opentelemetry.metrics import MeterProvider
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as ga
from opentelemetry.semconv._incubating.metrics import gen_ai_metrics as gm
from opentelemetry.trace import Status, StatusCode, TracerProvider
from sqlmodel import Field, Session, SQLModel, select

from trailwise.config import get_db_path, get_engine
from trailwise.schema import AgentSpan, LLMSpan, ToolSpan


class ExportConfig(SQLModel, table=True):
    """Whether the `gen_ai.*` OTel export adapter is opted in, per tenant."""

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: str | None = Field(default=None, index=True)
    gen_ai_semconv_enabled: bool = False
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# `gen_ai_semconv_enabled` is checked on every single span emission
# (feature 1.7's instrumentation call sites), so its result is cached
# in-process — a SQLite round trip per span would dominate the <5%
# latency-overhead budget (Stage 1 exit gate, feature 1.9) even when
# export is disabled. Keyed by (db path, tenant_id) so switching the
# configured store never serves another store's stale answer; a write
# through `set_export_config` invalidates the whole cache.
_export_config_cache: dict[tuple[str, str | None], ExportConfig] = {}


def _load_export_config(tenant_id: str | None) -> ExportConfig:
    with Session(get_engine()) as session:
        if tenant_id is not None:
            row = session.exec(
                select(ExportConfig).where(ExportConfig.tenant_id == tenant_id)
            ).first()
            if row is not None:
                return row
        row = session.exec(select(ExportConfig).where(ExportConfig.tenant_id.is_(None))).first()
        if row is not None:
            return row
    return ExportConfig()


def get_export_config(tenant_id: str | None = None) -> ExportConfig:
    """Resolve export config: tenant-specific row if present, else the
    global default row, else a built-in (not persisted, opted-out) default."""
    cache_key = (get_db_path(), tenant_id)
    cached = _export_config_cache.get(cache_key)
    if cached is not None:
        return cached
    row = _load_export_config(tenant_id)
    _export_config_cache[cache_key] = row
    return row


def set_export_config(*, tenant_id: str | None = None, gen_ai_semconv_enabled: bool) -> ExportConfig:
    """Create or update the export config row for `tenant_id` (None = global default)."""
    with Session(get_engine()) as session:
        row = session.exec(select(ExportConfig).where(ExportConfig.tenant_id == tenant_id)).first()
        if row is None:
            row = ExportConfig(tenant_id=tenant_id)
            session.add(row)
        row.gen_ai_semconv_enabled = gen_ai_semconv_enabled
        row.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(row)
        _export_config_cache.clear()
        return row


def gen_ai_semconv_enabled(tenant_id: str | None = None) -> bool:
    """Whether `tenant_id` (falling back to the global default) has opted into `gen_ai.*` export."""
    return get_export_config(tenant_id=tenant_id).gen_ai_semconv_enabled


def _to_epoch_ns(moment: datetime) -> int:
    return int(moment.timestamp() * 1_000_000_000)


def _set_correlation_attrs(otel_span: trace.Span, span: AgentSpan | ToolSpan | LLMSpan) -> None:
    otel_span.set_attribute("trailwise.schema_version", span.schema_version)
    otel_span.set_attribute("trailwise.trace_id", span.trace_id)
    otel_span.set_attribute("trailwise.span_id", span.span_id)
    if span.parent_span_id:
        otel_span.set_attribute("trailwise.parent_span_id", span.parent_span_id)
    if span.tenant_id:
        otel_span.set_attribute("trailwise.tenant_id", span.tenant_id)


def export_agent_span(span: AgentSpan, *, tracer_provider: TracerProvider | None = None) -> None:
    """Export an `AgentSpan` as an OTel span carrying `gen_ai.agent.*` attributes."""
    if not gen_ai_semconv_enabled(tenant_id=span.tenant_id):
        return

    tracer = trace.get_tracer(__name__, tracer_provider=tracer_provider)
    otel_span = tracer.start_span(
        f"{span.operation_name} {span.agent_name}",
        start_time=_to_epoch_ns(span.started_at),
    )
    otel_span.set_attribute(ga.GEN_AI_OPERATION_NAME, span.operation_name)
    otel_span.set_attribute(ga.GEN_AI_AGENT_ID, span.agent_id)
    otel_span.set_attribute(ga.GEN_AI_AGENT_NAME, span.agent_name)
    if span.description:
        otel_span.set_attribute(ga.GEN_AI_AGENT_DESCRIPTION, span.description)
    if span.conversation_id:
        otel_span.set_attribute(ga.GEN_AI_CONVERSATION_ID, span.conversation_id)
    # Not part of gen_ai.* (semconv has no loop-guard concept) — trailwise's
    # own runaway-loop signal (PRD §4, §7).
    otel_span.set_attribute("trailwise.loop_iteration", span.loop_iteration)
    _set_correlation_attrs(otel_span, span)

    end_time = _to_epoch_ns(span.ended_at) if span.ended_at else time.time_ns()
    otel_span.end(end_time=end_time)


def export_tool_span(span: ToolSpan, *, tracer_provider: TracerProvider | None = None) -> None:
    """Export a `ToolSpan` as an OTel span carrying `gen_ai.tool.*` attributes."""
    if not gen_ai_semconv_enabled(tenant_id=span.tenant_id):
        return

    tracer = trace.get_tracer(__name__, tracer_provider=tracer_provider)
    otel_span = tracer.start_span(
        f"execute_tool {span.tool_name}",
        start_time=_to_epoch_ns(span.started_at),
    )
    otel_span.set_attribute(ga.GEN_AI_OPERATION_NAME, "execute_tool")
    otel_span.set_attribute(ga.GEN_AI_TOOL_NAME, span.tool_name)
    otel_span.set_attribute(ga.GEN_AI_TOOL_TYPE, span.tool_type)
    if span.tool_call_id:
        otel_span.set_attribute(ga.GEN_AI_TOOL_CALL_ID, span.tool_call_id)
    if span.description:
        otel_span.set_attribute(ga.GEN_AI_TOOL_DESCRIPTION, span.description)
    if span.success is False:
        otel_span.set_status(Status(StatusCode.ERROR, description=span.error_message))
    _set_correlation_attrs(otel_span, span)

    end_time = _to_epoch_ns(span.ended_at) if span.ended_at else time.time_ns()
    otel_span.end(end_time=end_time)


def export_llm_span(
    span: LLMSpan,
    *,
    tracer_provider: TracerProvider | None = None,
    meter_provider: MeterProvider | None = None,
) -> None:
    """Export an `LLMSpan` as an OTel span carrying `gen_ai.request.*`/
    `gen_ai.response.*`/`gen_ai.usage.*` attributes, plus the
    `gen_ai.client.token.usage` and `gen_ai.client.operation.duration` metrics."""
    if not gen_ai_semconv_enabled(tenant_id=span.tenant_id):
        return

    tracer = trace.get_tracer(__name__, tracer_provider=tracer_provider)
    otel_span = tracer.start_span(
        f"{span.operation_name} {span.request_model}",
        start_time=_to_epoch_ns(span.started_at),
    )
    otel_span.set_attribute(ga.GEN_AI_OPERATION_NAME, span.operation_name)
    otel_span.set_attribute(ga.GEN_AI_PROVIDER_NAME, span.provider_name)
    otel_span.set_attribute(ga.GEN_AI_REQUEST_MODEL, span.request_model)
    if span.response_model:
        otel_span.set_attribute(ga.GEN_AI_RESPONSE_MODEL, span.response_model)
    if span.input_tokens is not None:
        otel_span.set_attribute(ga.GEN_AI_USAGE_INPUT_TOKENS, span.input_tokens)
    if span.output_tokens is not None:
        otel_span.set_attribute(ga.GEN_AI_USAGE_OUTPUT_TOKENS, span.output_tokens)
    if span.reasoning_tokens is not None:
        otel_span.set_attribute(ga.GEN_AI_USAGE_REASONING_OUTPUT_TOKENS, span.reasoning_tokens)
    if span.cache_read_input_tokens is not None:
        otel_span.set_attribute(ga.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS, span.cache_read_input_tokens)
    if span.cache_creation_input_tokens is not None:
        otel_span.set_attribute(ga.GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS, span.cache_creation_input_tokens)
    if span.finish_reasons:
        otel_span.set_attribute(ga.GEN_AI_RESPONSE_FINISH_REASONS, list(span.finish_reasons))
    if span.temperature is not None:
        otel_span.set_attribute(ga.GEN_AI_REQUEST_TEMPERATURE, span.temperature)
    if span.max_tokens is not None:
        otel_span.set_attribute(ga.GEN_AI_REQUEST_MAX_TOKENS, span.max_tokens)
    # Cost/prompt-version attribution: trailwise-specific, no gen_ai.* home yet.
    if span.estimated_cost_usd is not None:
        otel_span.set_attribute("trailwise.estimated_cost_usd", span.estimated_cost_usd)
    if span.prompt_version_id:
        otel_span.set_attribute("trailwise.prompt_version_id", span.prompt_version_id)
    _set_correlation_attrs(otel_span, span)

    start_ns = _to_epoch_ns(span.started_at)
    end_ns = _to_epoch_ns(span.ended_at) if span.ended_at else time.time_ns()
    otel_span.end(end_time=end_ns)

    _record_llm_metrics(span, meter_provider=meter_provider, duration_seconds=(end_ns - start_ns) / 1_000_000_000)


def _record_llm_metrics(span: LLMSpan, *, meter_provider: MeterProvider | None, duration_seconds: float) -> None:
    meter = metrics.get_meter(__name__, meter_provider=meter_provider)
    token_usage = gm.create_gen_ai_client_token_usage(meter)
    operation_duration = gm.create_gen_ai_client_operation_duration(meter)

    base_attrs = {
        ga.GEN_AI_OPERATION_NAME: span.operation_name,
        ga.GEN_AI_PROVIDER_NAME: span.provider_name,
        ga.GEN_AI_REQUEST_MODEL: span.request_model,
    }
    if span.input_tokens is not None:
        token_usage.record(
            span.input_tokens,
            {**base_attrs, ga.GEN_AI_TOKEN_TYPE: ga.GenAiTokenTypeValues.INPUT.value},
        )
    if span.output_tokens is not None:
        token_usage.record(
            span.output_tokens,
            {**base_attrs, ga.GEN_AI_TOKEN_TYPE: ga.GenAiTokenTypeValues.COMPLETION.value},
        )
    operation_duration.record(duration_seconds, base_attrs)
