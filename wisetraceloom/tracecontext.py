"""W3C Trace Context propagation (PRD §5, §7 — feature 1.8).

A single propagation format is used throughout: `traceparent`/`tracestate`
per the W3C Trace Context spec (`{version}-{trace-id}-{parent-id}-{trace-flags}`,
e.g. `00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01`). In-process,
the "current" trace/span id is carried via `contextvars` — the same
technique `wisetraceloom.logging.bind_context` uses — so it propagates
correctly across asyncio tasks without leaking between concurrently
running ones. Across a process boundary, `inject_traceparent`/
`extract_traceparent` move the same ids through a plain header dict.

`wisetraceloom.schema`'s `trace_id`/`span_id` fields and `wisetraceloom.instrumentation`'s
context managers both use `generate_trace_id`/`generate_span_id` here, so
ids are W3C-conformant (32/16 lowercase hex chars) from the moment a span
is created, not just at process boundaries.
"""

from __future__ import annotations

import contextlib
import contextvars
import re
import secrets
from typing import Iterator

TRACEPARENT_VERSION = "00"
TRACEPARENT_HEADER = "traceparent"
TRACESTATE_HEADER = "tracestate"

_TRACEPARENT_RE = re.compile(
    r"^(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)

_current_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "wisetraceloom_trace_id", default=None
)
_current_span_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "wisetraceloom_span_id", default=None
)
_current_tracestate: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "wisetraceloom_tracestate", default=None
)


def generate_trace_id() -> str:
    """A random 128-bit trace id: 32 lowercase hex chars (W3C-conformant)."""
    return secrets.token_hex(16)


def generate_span_id() -> str:
    """A random 64-bit span id: 16 lowercase hex chars (W3C-conformant)."""
    return secrets.token_hex(8)


def format_traceparent(trace_id: str, span_id: str, *, sampled: bool = True) -> str:
    """Render a `traceparent` header value."""
    flags = "01" if sampled else "00"
    return f"{TRACEPARENT_VERSION}-{trace_id}-{span_id}-{flags}"


def parse_traceparent(header: str) -> tuple[str, str, bool] | None:
    """Parse a `traceparent` header. Returns `(trace_id, span_id, sampled)`,
    or `None` if malformed or carrying an all-zero id — callers should treat
    that as "no incoming trace context" rather than raise, matching the
    fail-open posture used everywhere else external/untrusted data enters.
    """
    match = _TRACEPARENT_RE.match(header.strip())
    if match is None:
        return None
    trace_id = match.group("trace_id")
    span_id = match.group("span_id")
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    sampled = bool(int(match.group("flags"), 16) & 0x01)
    return trace_id, span_id, sampled


def current_trace_id() -> str | None:
    return _current_trace_id.get()


def current_span_id() -> str | None:
    return _current_span_id.get()


def current_tracestate() -> str | None:
    return _current_tracestate.get()


@contextlib.contextmanager
def bound_trace_context(
    trace_id: str, span_id: str, *, tracestate: str | None = None
) -> Iterator[None]:
    """Bind `(trace_id, span_id[, tracestate])` as "current" for the
    duration of the block — asyncio-task-local via `contextvars`, so
    concurrent tasks never see each other's trace context."""
    trace_token = _current_trace_id.set(trace_id)
    span_token = _current_span_id.set(span_id)
    state_token = _current_tracestate.set(tracestate)
    try:
        yield
    finally:
        _current_trace_id.reset(trace_token)
        _current_span_id.reset(span_token)
        _current_tracestate.reset(state_token)


def inject_traceparent(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of `headers` with `traceparent`/`tracestate` added from
    the current in-process context, for an outbound call across a process
    boundary (e.g. to another agent/service). A no-op copy if no trace
    context is currently bound."""
    trace_id = current_trace_id()
    span_id = current_span_id()
    if trace_id is None or span_id is None:
        return dict(headers)

    result = dict(headers)
    result[TRACEPARENT_HEADER] = format_traceparent(trace_id, span_id)
    tracestate = current_tracestate()
    if tracestate:
        result[TRACESTATE_HEADER] = tracestate
    return result


def extract_traceparent(headers: dict[str, str]) -> tuple[str, str, bool] | None:
    """Parse `traceparent` (if present and valid) out of an inbound header dict."""
    header = headers.get(TRACEPARENT_HEADER)
    if header is None:
        return None
    return parse_traceparent(header)
