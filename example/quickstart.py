"""Runnable end-to-end demo of wisetraceloom's public API.

Mirrors the README quickstart, plus rotation/redaction/OTel-export/prompt
versioning/context-propagation, using fake `search`/`call_llm` stand-ins so
this runs with zero external services.

Run from the repo root (as a module, so `wisetraceloom/` resolves off the repo
root rather than the script's own directory):

    uv run python -m example.quickstart
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import wisetraceloom
from wisetraceloom.config import set_rotation_config
from wisetraceloom.otel_export import set_export_config
from wisetraceloom.prompts import register_prompt_version
from wisetraceloom.tracecontext import extract_traceparent, inject_traceparent


# --- fake external calls, so the example needs no network/API key --------


def search(query: str) -> list[str]:
    return [f"result for {query!r}"]


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int


@dataclass
class _LLMResponse:
    usage: _Usage
    text: str


def call_llm(context: list[str]) -> _LLMResponse:
    return _LLMResponse(usage=_Usage(input_tokens=42, output_tokens=17), text="Sunny, 28C.")


# --- 1. configure logging (console output; swap json_output=True for prod) -

# Rotation config is persisted (SQLite), so a `log_file_path` set by *any*
# prior run of this or another wisetraceloom script is still the active global
# default the next time `configure()` resolves a destination. Create the
# directory unconditionally so that's never a surprise on rerun.
Path("logs").mkdir(exist_ok=True)

wisetraceloom.configure(json_output=False)

logger = wisetraceloom.get_logger("example.quickstart")


# --- 2. optional config: rotation, PII redaction model, OTel export opt-in -

set_rotation_config(
    log_file_path="logs/wisetraceloom.log",
    max_size_mb=50.0,
    rotation_interval="midnight",
    backup_count=7,
    compress_backups=True,
)

set_export_config(gen_ai_semconv_enabled=True)  # emit gen_ai.* OTel spans/metrics too


# --- 3. register a prompt version once, reuse its id on every llm_call -----

prompt_version = register_prompt_version(
    "router_agent.system_prompt",
    "You are a helpful routing agent that answers weather questions.",
    model_params={"temperature": 0.2},
)
print(f"Registered {prompt_version.title}")


# --- 4. the core instrumentation: nested agent_step / tool_call / llm_call -

with wisetraceloom.bind_context(tenant_id="acme", request_id="req-001"):
    with wisetraceloom.agent_step(agent_id="router-1", agent_name="router_agent") as agent:
        agent.description = "Answer a weather question"

        with wisetraceloom.tool_call("web_search") as tool:
            results = search("weather in Bengaluru")
            tool.description = "Search the web"

        with wisetraceloom.llm_call(
            "anthropic", "claude-sonnet-5", prompt_version_id=prompt_version.title
        ) as llm:
            response = call_llm(results)
            llm.input_tokens = response.usage.input_tokens
            llm.output_tokens = response.usage.output_tokens

    logger.info("weather_answer", answer=response.text, tenant="acme")


# --- 5. decorator form, for when one function *is* one step ----------------


@wisetraceloom.trace_tool_call("summarize")
def summarize(text: str) -> str:
    return text[:20]


summarize("This sentence will get truncated for the summary tool span.")


# --- 6. propagate the trace across a simulated process boundary ------------

with wisetraceloom.agent_step(agent_id="router-1", agent_name="router_agent"):
    headers = inject_traceparent({"content-type": "application/json"})
    print(f"Outbound headers: {headers}")

    # ... elsewhere, in the "downstream" service that received `headers`:
    incoming = extract_traceparent(headers)
    if incoming:
        trace_id, span_id, sampled = incoming
        with wisetraceloom.tracecontext.bound_trace_context(trace_id, span_id):
            logger.info("resumed_incoming_trace", trace_id=trace_id, sampled=sampled)


print("Done. Check console output above for structured span/log events.")
