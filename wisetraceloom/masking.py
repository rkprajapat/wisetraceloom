"""Server-side fail-closed masking callback (PRD §3, §7 — feature 2.6).

Distinct from feature 1.4's client-side redaction, which runs in-process
inside the structlog processor pipeline before a log line is ever rendered
anywhere. That pipeline never sees storage's own write path: feature 2.1's
`append_commit` (called directly, or via `enqueue_append` from
`instrumentation.py`'s `_emit_span`) persists a span's `model_dump()` dict
straight to SQLite, never through `wisetraceloom.logging.configure()`'s
processors. So a masking failure in this second, parallel write path has no
safety net unless the storage boundary enforces one itself — that boundary
is this module.

`wisetraceloom.storage.append_commit` runs `apply_masking` over every
payload before it is written. Unlike feature 1.5's fail-open instrumentation
posture, a masking failure here **blocks the write** — it raises rather than
falling back to persisting the payload unmasked, mirroring feature 1.4's own
fail-closed stance on redaction and Langfuse's masking-callback design (PRD
Key Findings) as the server-side safety net PRD §3 calls for behind
client-side redaction.

The default callback reuses feature 1.4's structured-field-name + regex
tiers (cheap, dependency-free, always active). A host can register a
stricter one (e.g. layering in Presidio, or per-tenant rules) via
`set_masking_callback` — the same caller-supplied-callable precedent as
feature 2.3's `AnchorSink`: this module ships a sensible default rather than
no default, since unlike an external anchor a masking policy the host never
customizes is still a real safety net, not a placebo.
"""

from __future__ import annotations

from typing import Any, Callable

from wisetraceloom.redaction import redact_regex_matches, redact_structured_fields

MaskingCallback = Callable[[dict[str, Any]], dict[str, Any]]


class MaskingError(Exception):
    """Raised when a masking callback fails, or returns something other
    than a dict. Fail-closed: whoever raised this must not go on to store
    the payload that was being masked."""


def default_masking_callback(payload: dict[str, Any]) -> dict[str, Any]:
    """Structured field-name redaction, then regex scrubbing over the
    remaining string values — the same default tiers feature 1.4 runs in
    the structlog pipeline, reapplied here since storage payloads never
    pass through that pipeline."""
    payload = redact_structured_fields(payload)
    for key, value in payload.items():
        if isinstance(value, str):
            payload[key] = redact_regex_matches(value)
    return payload


_masking_callback: MaskingCallback = default_masking_callback


def set_masking_callback(callback: MaskingCallback | None) -> None:
    """Register the callback `apply_masking` runs before every storage
    write. `None` resets to `default_masking_callback`."""
    global _masking_callback
    _masking_callback = callback or default_masking_callback


def get_masking_callback() -> MaskingCallback:
    return _masking_callback


def apply_masking(payload: dict[str, Any]) -> dict[str, Any]:
    """Run the registered masking callback over `payload`. Fail-closed: any
    exception the callback raises, or a non-dict return value, is wrapped in
    `MaskingError` and re-raised — never swallowed, never substituted with
    the unmasked payload. `wisetraceloom.storage.append_commit` lets this
    propagate rather than falling back to persisting `payload` unmasked."""
    try:
        masked = _masking_callback(payload)
    except Exception as exc:
        raise MaskingError(f"masking callback raised: {exc}") from exc
    if not isinstance(masked, dict):
        raise MaskingError(f"masking callback returned {type(masked).__name__}, expected dict")
    return masked
