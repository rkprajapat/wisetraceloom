"""Fail-open wrapper around SDK instrumentation (PRD §5, §7).

The logging/instrumentation SDK must never crash the host application:
anything raised *by trailwise's own instrumentation code* (span
construction, export, storage) is caught and logged, never propagated.
This is the opposite policy from PII masking (feature 1.4), which is
fail-**closed** by design — the two are deliberately asymmetric: a failure
to observe should never take down the host, but a failure to redact must
never let unmasked PII through.

Only `Exception` is caught, not `BaseException` — `KeyboardInterrupt`,
`SystemExit`, and `GeneratorExit` are control-flow signals, not
instrumentation errors, and swallowing them would make the host
unresponsive to shutdown/interrupt.

This wrapper protects instrumentation internals; it must never be placed
around the host's own business logic (the code inside a `with
trailwise.tool_call(...):` block, say) — that code's exceptions are the
host's to handle, not trailwise's to swallow.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
from typing import Any, Callable, Iterator, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def _log_swallowed_exception(operation: str, exc: Exception) -> None:
    # PRD §7: silently not logging for a long period is itself unacceptable
    # for audit/compliance, so the failure is surfaced via its own
    # telemetry — but that telemetry call is itself sandboxed, since a
    # broken logging pipeline must not turn "fail open" into "fail crash".
    try:
        from trailwise.logging import get_logger

        get_logger("trailwise.failsafe").warning(
            "trailwise_instrumentation_error",
            operation=operation,
            exc_type=type(exc).__name__,
            exc_message=str(exc),
        )
    except Exception:
        pass


def fail_open(operation: str | None = None) -> Callable[[F], F]:
    """Decorator: catch any `Exception` raised by the wrapped function,
    log it, and return `None` instead of propagating. Works on both sync
    and async callables.
    """

    def decorator(func: F) -> F:
        op_name = operation or func.__qualname__

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    _log_swallowed_exception(op_name, exc)
                    return None

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                _log_swallowed_exception(op_name, exc)
                return None

        return sync_wrapper  # type: ignore[return-value]

    return decorator


@contextlib.contextmanager
def fail_open_context(operation: str) -> Iterator[None]:
    """Context-manager form of `fail_open`, for wrapping a block of
    instrumentation code (span construction + export) rather than a whole
    function — e.g. inside `trailwise.instrumentation`'s span context
    managers, around the setup/teardown code, never around the host's own
    business logic in the `with` block.
    """
    try:
        yield
    except Exception as exc:
        _log_swallowed_exception(operation, exc)
