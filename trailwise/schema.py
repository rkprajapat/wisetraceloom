"""Internal rich schema for agentic spans, evaluation scores, and cost data.

This is the "custom rich schema" side of the hybrid architecture in PRD §2:
it captures agentic semantics (loop iterations, eval scores, cost/token
attribution) that the OTel GenAI conventions don't yet model well, while
field names mirror the corresponding `gen_ai.*` attributes 1:1 so the future
export adapter (feature 1.3) can map one to the other without lossy
translation.

**Versioning.** `SCHEMA_VERSION` follows PRD §7's schema-versioning
guidance, itself modeled on OTel's stability opt-in pattern
(`OTEL_SEMCONV_STABILITY_OPT_IN`): every span/score model stamps its
`schema_version` at construction time, so records already written keep the
version they were created under even after this module evolves. Bump
`SCHEMA_VERSION` (semver) whenever a field is added, renamed, or removed in
a way that changes wire compatibility; additive, backward-compatible fields
(new optional field, default provided) don't require a bump.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0.0"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _SpanBase(BaseModel):
    """Fields shared by every span type: identity, trace linkage, timing."""

    schema_version: str = SCHEMA_VERSION
    span_id: str
    trace_id: str
    parent_span_id: str | None = None
    tenant_id: str | None = None
    started_at: datetime = Field(default_factory=_utcnow)
    ended_at: datetime | None = None


class AgentSpan(_SpanBase):
    """An agent step/invocation. Mirrors `gen_ai.agent.*` + `gen_ai.conversation.id`.

    `loop_iteration` is the agent's own reasoning-loop counter (PRD §4, §7) —
    the field a runaway-loop guard reads to cut off an agent that never
    converges, rather than something derived after the fact from span count.
    """

    agent_id: str
    agent_name: str
    description: str | None = None
    conversation_id: str | None = None
    operation_name: Literal["create_agent", "invoke_agent", "invoke_workflow", "plan"]
    loop_iteration: int = 0


class ToolSpan(_SpanBase):
    """A tool call. Mirrors `gen_ai.tool.*`."""

    tool_name: str
    tool_call_id: str | None = None
    tool_type: Literal["function", "extension"] = "function"
    description: str | None = None
    success: bool | None = None
    error_message: str | None = None


class LLMSpan(_SpanBase):
    """An LLM call. Mirrors `gen_ai.request.*` / `gen_ai.response.*` / `gen_ai.usage.*`.

    Token fields are split per PRD §4's four token layers (here: prompt,
    cache read, cache creation, and response — reasoning is its own field
    since providers price/report it separately) plus an estimated USD cost
    for per-tenant attribution (PRD §7); all are optional since not every
    provider reports every field.
    """

    provider_name: str
    request_model: str
    response_model: str | None = None
    operation_name: str = "chat"
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    finish_reasons: list[str] = Field(default_factory=list)
    temperature: float | None = None
    max_tokens: int | None = None
    estimated_cost_usd: float | None = None
    prompt_version_id: str | None = None


class EvalScore(BaseModel):
    """A single evaluation result, sliceable by `prompt_version_id` (PRD §8.3)."""

    schema_version: str = SCHEMA_VERSION
    trace_id: str
    span_id: str | None = None
    metric_name: str
    score: float
    threshold: float | None = None
    passed: bool | None = None
    prompt_version_id: str | None = None
    dataset_name: str | None = None
    evaluated_at: datetime = Field(default_factory=_utcnow)
