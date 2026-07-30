"""structlog-based capture pipeline.

Context (tenant_id, correlation_id, etc.) is bound via `contextvars` so it
propagates correctly across asyncio tasks without leaking between
concurrently running tasks (each asyncio Task gets its own copy of the
contextvars context).
"""

from __future__ import annotations

import contextlib
import logging as _stdlib_logging
from typing import Any, Iterator

import structlog

from trailwise.config import get_rotation_config
from trailwise.rotation import build_rotating_handler

_configured = False
_active_file_handler: _stdlib_logging.Handler | None = None


def configure(
    *,
    json_output: bool = True,
    file_path: str | None = None,
    tenant_id: str | None = None,
) -> None:
    """Configure the global structlog pipeline. Safe to call more than once.

    The log destination resolves as: explicit `file_path` argument, else the
    `log_file_path` stored in the `RotationConfig` row for `tenant_id`
    (falling back to the global default row), else stdout. When a file
    destination is resolved, events are routed through a stdlib `logging`
    root logger carrying a rotating file handler built from that same
    `RotationConfig` — see `trailwise.config` and `trailwise.rotation`.
    """
    global _configured, _active_file_handler

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer(),
    ]

    rotation_config = get_rotation_config(tenant_id=tenant_id)
    effective_file_path = file_path if file_path is not None else rotation_config.log_file_path

    root = _stdlib_logging.getLogger()
    if _active_file_handler is not None:
        # Only ever touch the handler we previously added ourselves — the
        # root logger may carry handlers owned by the host app or pytest.
        root.removeHandler(_active_file_handler)
        _active_file_handler.close()
        _active_file_handler = None

    if effective_file_path is not None:
        handler = build_rotating_handler(effective_file_path, rotation_config)
        handler.setFormatter(_stdlib_logging.Formatter("%(message)s"))

        if root.level == _stdlib_logging.NOTSET or root.level > _stdlib_logging.INFO:
            root.setLevel(_stdlib_logging.INFO)
        root.addHandler(handler)
        _active_file_handler = handler

        logger_factory = structlog.stdlib.LoggerFactory()
        wrapper_class: Any = structlog.stdlib.BoundLogger
    else:
        logger_factory = structlog.PrintLoggerFactory()
        wrapper_class = structlog.make_filtering_bound_logger(20)  # INFO

    structlog.configure(
        processors=processors,
        wrapper_class=wrapper_class,
        context_class=dict,
        logger_factory=logger_factory,
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> Any:
    """Return a structlog bound logger, configuring the pipeline on first use."""
    if not _configured:
        configure()
    return structlog.get_logger(name)


@contextlib.contextmanager
def bind_context(**kwargs: Any) -> Iterator[None]:
    """Bind key/value pairs to the logging context for the current asyncio task."""
    with structlog.contextvars.bound_contextvars(**kwargs):
        yield
