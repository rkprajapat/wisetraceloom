"""One-line instrumentation for the three common call sites (PRD §5 —
feature 1.7): an agent step, a tool call, an LLM call. Each is a context
manager yielding the matching `wisetraceloom.schema` span so the caller can
fill in fields as they become known (e.g. an `LLMSpan`'s token counts,
read off the provider's response, inside the `with` block); a decorator
wrapping the same context manager around an entire function is also
provided for the common case where one function *is* one step.

Every context manager:

1. Resolves `trace_id`/`parent_span_id` from `wisetraceloom.tracecontext`'s
   current context (starting a new trace if none is active) and generates
   a new `span_id`, then binds `(trace_id, span_id)` as current for the
   duration of the block — so a `tool_call` or `llm_call` nested inside an
   `agent_step` is automatically parented to it, no manual id-passing.
2. Stamps `ended_at` and emits the span (structured log event + OTel
   export) on exit, success or failure.
3. Never swallows the caller's own exceptions — only the emit step
   (logging + export) is wrapped in `wisetraceloom.failsafe.fail_open_context`,
   per feature 1.5's boundary: instrumentation failures are fail-open, the
   host's business logic is not wisetraceloom's to swallow.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, TypeVar

from wisetraceloom.cost import check_spend_anomaly, estimate_cost_usd, record_spend
from wisetraceloom.failsafe import fail_open_context
from wisetraceloom.logging import get_logger
from wisetraceloom.otel_export import export_agent_span, export_llm_span, export_tool_span
from wisetraceloom.schema import AgentSpan, LLMSpan, ToolSpan
from wisetraceloom.storage import enqueue_append
from wisetraceloom.tracecontext import (
    bound_trace_context,
    current_span_id,
    current_trace_id,
    generate_span_id,
    generate_trace_id,
)

F = TypeVar("F", bound=Callable[..., Any])

_SPAN_EVENT_NAMES = {
    AgentSpan: "agent_span",
    ToolSpan: "tool_span",
    LLMSpan: "llm_span",
}


def _emit_span(span: AgentSpan | ToolSpan | LLMSpan, exporter: Callable[..., None]) -> None:
    event_name = _SPAN_EVENT_NAMES[type(span)]
    with fail_open_context(f"emit_span:{type(span).__name__}"):
        get_logger("wisetraceloom.spans").info(event_name, **span.model_dump(mode="json"))
        exporter(span)
    # Independent fail-open block: a storage failure never blocks (or is
    # blocked by) the log/export step above, and vice versa. enqueue_append
    # (not append_commit) is used here deliberately — a synchronous durable
    # write costs a few ms even on a fast SQLite path, which doesn't fit the
    # Stage 1 exit gate's <5% latency budget on every span (see storage.py's
    # module docstring for the durability trade-off this implies).
    with fail_open_context(f"store_span:{type(span).__name__}"):
        enqueue_append(
            stream_id="spans",
            record_type=event_name,
            payload=span.model_dump(mode="json"),
            tenant_id=span.tenant_id,
        )


@contextlib.contextmanager
def agent_step(
    agent_id: str,
    agent_name: str,
    *,
    operation_name: str = "invoke_agent",
    conversation_id: str | None = None,
    tenant_id: str | None = None,
    description: str | None = None,
    loop_iteration: int = 0,
) -> Iterator[AgentSpan]:
    """Instrument one agent step. Yields the `AgentSpan` — mutate
    `span.loop_iteration` inside the block if it changes mid-step."""
    trace_id = current_trace_id() or generate_trace_id()
    parent_span_id = current_span_id()
    span = AgentSpan(
        span_id=generate_span_id(),
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        agent_id=agent_id,
        agent_name=agent_name,
        operation_name=operation_name,
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        description=description,
        loop_iteration=loop_iteration,
    )
    with bound_trace_context(trace_id, span.span_id):
        try:
            yield span
        finally:
            span.ended_at = datetime.now(timezone.utc)
            _emit_span(span, export_agent_span)


@contextlib.contextmanager
def tool_call(
    tool_name: str,
    *,
    tool_type: str = "function",
    tool_call_id: str | None = None,
    tenant_id: str | None = None,
    description: str | None = None,
) -> Iterator[ToolSpan]:
    """Instrument one tool call. Yields the `ToolSpan`; `success` is set to
    `True` if the block completes without raising, `False` (with
    `error_message`) if it raises — the exception itself still propagates."""
    trace_id = current_trace_id() or generate_trace_id()
    parent_span_id = current_span_id()
    span = ToolSpan(
        span_id=generate_span_id(),
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        tool_name=tool_name,
        tool_type=tool_type,
        tool_call_id=tool_call_id,
        tenant_id=tenant_id,
        description=description,
    )
    with bound_trace_context(trace_id, span.span_id):
        try:
            yield span
            if span.success is None:
                span.success = True
        except Exception as exc:
            span.success = False
            span.error_message = str(exc)
            raise
        finally:
            span.ended_at = datetime.now(timezone.utc)
            _emit_span(span, export_tool_span)


@contextlib.contextmanager
def llm_call(
    provider_name: str,
    request_model: str,
    *,
    operation_name: str = "chat",
    tenant_id: str | None = None,
    prompt_version_id: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> Iterator[LLMSpan]:
    """Instrument one LLM call. Yields the `LLMSpan` — set
    `span.input_tokens`/`output_tokens`/etc. inside the block once the
    provider's response is known."""
    trace_id = current_trace_id() or generate_trace_id()
    parent_span_id = current_span_id()
    span = LLMSpan(
        span_id=generate_span_id(),
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        provider_name=provider_name,
        request_model=request_model,
        operation_name=operation_name,
        tenant_id=tenant_id,
        prompt_version_id=prompt_version_id,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    with bound_trace_context(trace_id, span.span_id):
        try:
            yield span
        finally:
            span.ended_at = datetime.now(timezone.utc)
            _attribute_cost(span)
            _emit_span(span, export_llm_span)


def _attribute_cost(span: LLMSpan) -> None:
    """Auto-fill `span.estimated_cost_usd` from `wisetraceloom.cost`'s
    pricing config if the host hasn't already set it, then attribute the
    resulting cost to the span's tenant and check it against that tenant's
    rolling spend baseline. Cost attribution is instrumentation, not
    business logic, so — like `_emit_span` — this is wrapped in its own
    `fail_open_context`, independent of span emission/storage: a pricing
    lookup failure must never block a span from being logged or exported,
    and vice versa (feature 1.5's fail-open boundary)."""
    with fail_open_context("attribute_cost"):
        if span.estimated_cost_usd is None:
            span.estimated_cost_usd = estimate_cost_usd(
                span.provider_name,
                span.request_model,
                tenant_id=span.tenant_id,
                input_tokens=span.input_tokens,
                output_tokens=span.output_tokens,
                cache_read_input_tokens=span.cache_read_input_tokens,
                cache_creation_input_tokens=span.cache_creation_input_tokens,
            )
        if span.estimated_cost_usd is not None and span.tenant_id is not None:
            record_spend(span.tenant_id, span.estimated_cost_usd)
            check_spend_anomaly(span.tenant_id)


def _wrap(context_manager_factory: Callable[..., Any]) -> Callable[..., Callable[[F], F]]:
    """Build a decorator factory from a `*args, **kwargs -> context manager`
    callable, so `@trace_tool_call("search")` runs the whole decorated
    function inside `tool_call("search")`."""

    def decorator_factory(*cm_args: Any, **cm_kwargs: Any) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            if inspect.iscoroutinefunction(func):

                @functools.wraps(func)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    with context_manager_factory(*cm_args, **cm_kwargs):
                        return await func(*args, **kwargs)

                return async_wrapper  # type: ignore[return-value]

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                with context_manager_factory(*cm_args, **cm_kwargs):
                    return func(*args, **kwargs)

            return sync_wrapper  # type: ignore[return-value]

        return decorator

    return decorator_factory


trace_agent_step = _wrap(agent_step)
trace_tool_call = _wrap(tool_call)
trace_llm_call = _wrap(llm_call)
