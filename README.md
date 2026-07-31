# Trailwise

Enterprise-scale logging & observability SDK for agentic AI (Python).

Trailwise captures a rich internal schema for agent/tool/LLM calls
(loop iterations, eval scores, cost/token attribution) and exports it as
OpenTelemetry `gen_ai.*` spans/metrics for interoperability with the wider
observability ecosystem (Jaeger, Tempo, Grafana, Datadog, ...) — without
locking you into it. See [docs/prd.md](docs/prd.md) for the full design
rationale and [docs/development_status.md](docs/development_status.md) for
what's built.

**No environment variables.** Every knob in this SDK — the database file,
export opt-in, redaction settings, log rotation — is set through a Python
call, not `os.environ`. Configuration is explicit, in your own code, and
(except the database path itself) persisted in SQLite via SQLModel so it
survives restarts and can be inspected/audited like any other data.

## Install

```bash
uv add trailwise
# or, for the optional Presidio NER redaction layer:
uv add "trailwise[presidio]"
```

Requires Python ≥ 3.14.

## Quickstart

```python
import trailwise

trailwise.configure()  # console output by default; see "Logging" below

with trailwise.agent_step(agent_id="router-1", agent_name="router_agent") as agent:
    with trailwise.tool_call("web_search") as tool:
        results = search("weather in Bengaluru")
        tool.description = "Search the web"

    with trailwise.llm_call("anthropic", "claude-sonnet-5") as llm:
        response = call_llm(results)
        llm.input_tokens = response.usage.input_tokens
        llm.output_tokens = response.usage.output_tokens
```

That's it — each `with` block:

- generates W3C-conformant trace/span ids and auto-parents nested calls
  (the `tool_call`/`llm_call` above are automatically children of `agent`,
  no manual id-passing),
- redacts PII from anything logged,
- emits a structured log event (`agent_span` / `tool_span` / `llm_span`),
- exports an OTel `gen_ai.*` span (once you opt in — see below),
- and **never crashes your app**, even if the exporter or logging backend
  itself is down.

Prefer decorating a whole function instead of a `with` block? Use the
decorator form:

```python
@trailwise.trace_tool_call("web_search")
def search(query: str) -> list[str]:
    ...
```

(Works on both sync and async functions.)

## Logging

```python
trailwise.configure(
    json_output=True,       # False -> human-readable console output
    file_path=None,         # None -> stdout; or a path to write JSON lines to a file
    tenant_id=None,          # which tenant's rotation/export/redaction config to resolve
)

logger = trailwise.get_logger("my.module")
logger.info("something happened", extra_field="value")

with trailwise.bind_context(tenant_id="acme", request_id="abc123"):
    logger.info("scoped to this request")  # both fields attached automatically
```

`bind_context` uses `contextvars`, so it's safe across concurrent `asyncio`
tasks — one task's bound context never leaks into another's.

### Log rotation

Persisted in SQLite (see "Where configuration lives" below):

```python
from trailwise.config import set_rotation_config

set_rotation_config(
    log_file_path="logs/trailwise.log",
    max_size_mb=50.0,          # rotate at 50MB ...
    rotation_interval="midnight",  # ... or at midnight, whichever fires first
    backup_count=7,
    compress_backups=True,     # gzip rotated files
)
```

Per-tenant overrides: pass `tenant_id=...` to `set_rotation_config` /
`configure`; lookups fall back tenant-specific → global default → a
built-in default (50MB / midnight / 7 backups, no compression).

## PII redaction

Two tiers are **on by default**, no configuration needed:

1. **Structured field-name redaction** — any log field whose *key* matches
   a known-sensitive name (`password`, `api_key`, `ssn`, `email`, `phone`,
   `credit_card`, ... — see `trailwise.redaction.SENSITIVE_FIELD_NAMES`) is
   replaced with `[REDACTED]`, regardless of type.
2. **Regex scrubbing** — emails, phone numbers, and card-like digit runs
   inside any string field (including the log message itself) are masked.

A third, optional tier layers free-text NER redaction on top via
[Presidio](https://microsoft.github.io/presidio/):

```bash
uv add "trailwise[presidio]"
```

```python
from trailwise.redaction import presidio_available, redact_with_presidio

if presidio_available():
    redact_with_presidio("My name is John Smith")  # -> "My name is <PERSON>"
```

`presidio_available()` returns `False` gracefully if the extra isn't
installed — your app never crashes because of a missing optional
dependency.

Which spaCy model backs Presidio is configurable per tenant:

```python
from trailwise.redaction import set_redaction_config

set_redaction_config(presidio_spacy_model="en_core_web_lg")  # default: en_core_web_sm
```

Presidio does not guarantee catching all PII — treat it as one layer in
defense-in-depth, not a substitute for tiers 1–2.

## OTel `gen_ai.*` export

`gen_ai.*` is still an OTel *Development*-status semantic convention, so
export is opt-in, off by default:

```python
from trailwise.otel_export import set_export_config

set_export_config(gen_ai_semconv_enabled=True)              # global default
set_export_config(tenant_id="acme", gen_ai_semconv_enabled=True)  # per tenant
```

Once opted in, every `agent_step`/`tool_call`/`llm_call` also emits a
matching OTel span (`gen_ai.agent.*`/`gen_ai.tool.*`/`gen_ai.request.*`
etc.) plus `gen_ai.client.token.usage` and `gen_ai.client.operation.duration`
metrics, on whatever `TracerProvider`/`MeterProvider` your app has already
configured (or the OTel default no-op providers if it hasn't).

## Prompt versioning

Fingerprint a prompt template and auto-register a version — the same
template + model params always resolves to the same version, no manual
bumping:

```python
from trailwise.prompts import register_prompt_version

version = register_prompt_version(
    "router_agent.system_prompt",
    "You are a helpful routing agent...",
    model_params={"temperature": 0.2},
)
print(version.title)  # "router_agent.system_prompt — v1 — 2026-07-30T18:00Z"

with trailwise.llm_call("anthropic", "claude-sonnet-5", prompt_version_id=version.title) as llm:
    ...
```

Re-running the same template (even with different runtime variable
values substituted elsewhere) links to the same version rather than
minting a new one; changing the template text or model params registers a
new, incremented version automatically.

## Trace propagation across process boundaries

Trailwise uses a single propagation format throughout: [W3C Trace
Context](https://www.w3.org/TR/trace-context/) (`traceparent`/
`tracestate`). To carry a trace across an outbound HTTP call to another
agent/service:

```python
import httpx
import trailwise
from trailwise.tracecontext import inject_traceparent, extract_traceparent

# Outbound: propagate the current trace context
headers = inject_traceparent({"content-type": "application/json"})
httpx.post("https://other-service/agent", headers=headers, json=payload)

# Inbound (in the other service): resume the incoming trace
incoming = extract_traceparent(request.headers)
if incoming:
    trace_id, span_id, sampled = incoming
    with trailwise.tracecontext.bound_trace_context(trace_id, span_id):
        ...  # spans created in here are parented to the incoming trace
```

## Where configuration lives

Every domain's settings are a SQLModel table in one shared SQLite file —
inspectable, queryable, and persisted like any other data:

| Domain | Table | Set with |
|---|---|---|
| Log rotation | `RotationConfig` | `trailwise.config.set_rotation_config(...)` |
| OTel export opt-in | `ExportConfig` | `trailwise.otel_export.set_export_config(...)` |
| Presidio model | `RedactionConfig` | `trailwise.redaction.set_redaction_config(...)` |
| Prompt versions | `PromptVersion` | `trailwise.prompts.register_prompt_version(...)` |

All of the above (except prompt versions) support a `tenant_id` — pass one
to scope the setting to a tenant; omit it to set the global default that
tenants without their own override fall back to.

The one exception is the database file's own path, since it can't be
stored inside the database it names:

```python
trailwise.set_db_path("/var/lib/myapp/trailwise.db")  # call before first use
```

Defaults to `.trailwise/trailwise.db` (relative to the working directory)
if never called.

## Fail-open guarantee

Trailwise's own instrumentation (span construction, logging, export) never
crashes your host application — any exception it raises internally is
caught, logged as a `trailwise_instrumentation_error` warning event, and
swallowed. This is *only* about trailwise's own code: an exception your
own code raises inside a `with trailwise.tool_call(...):` block still
propagates normally — trailwise never swallows your bugs, only its own
instrumentation failures.

This is deliberately the opposite policy from PII redaction, which is
fail-**closed**: a masking failure blocks the event rather than risk
storing unmasked PII.

## Development

```bash
uv sync --group dev              # + `--extra presidio` for the NER redaction tests
uv run python -m pytest tests/
```

See [docs/development_status.md](docs/development_status.md) for
feature-by-feature status against [docs/prd.md](docs/prd.md), and
[CLAUDE.md](CLAUDE.md) for the code-review-graph MCP tooling used in this
repo.
