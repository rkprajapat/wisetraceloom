"""SQLite-backed configuration store (SQLModel), scoped to log destination
and rotation for now.

Every row carries a `tenant_id` so a per-tenant configuration manager can be
layered on top later without a schema migration; today only the global row
(tenant_id=None) is written by default, and lookups fall back to it when no
tenant-specific row exists.

Wisetraceloom takes no configuration from environment variables anywhere in the
SDK — every knob, including where this very store's SQLite file lives, is
set through a Python call (`set_db_path`, `set_rotation_config`, etc.) so a
host app's configuration is explicit and traceable to one place in its own
code. The database path is the one exception that can't itself live *in*
the database (opening the file requires knowing its path first); it's a
plain settable module-level default instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from sqlmodel import Field, Session, SQLModel, create_engine, select

DEFAULT_DB_PATH = ".wisetraceloom/wisetraceloom.db"

_db_path_override: str | None = None


def set_db_path(path: str | None) -> None:
    """Override the SQLite file every domain's config tables share (rotation
    config, prompt versions, export config, redaction config, ...). Pass
    `None` to reset to `DEFAULT_DB_PATH`. Call before first use — engines
    are cached per path, so switching paths mid-process opens a second,
    independent database rather than migrating the first.
    """
    global _db_path_override
    _db_path_override = path


def get_db_path() -> str:
    return _db_path_override if _db_path_override is not None else DEFAULT_DB_PATH


class RotationConfig(SQLModel, table=True):
    """Log file destination and rotation settings.

    `log_file_path` is the configured log destination; when unset, callers
    fall back to console/stdout output instead. `max_size_mb` and
    `rotation_interval` are independent, combinable triggers: rotation
    happens on whichever fires first. Either may be left unset to disable
    that trigger.
    """

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: str | None = Field(default=None, index=True)
    log_file_path: str | None = None
    max_size_mb: float | None = None
    # logging.handlers.TimedRotatingFileHandler `when` values: S, M, H, D,
    # W0-W6, midnight
    rotation_interval: str | None = None
    backup_count: int = 7
    compress_backups: bool = False
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def _resolve_db_path() -> str:
    return get_db_path()


@lru_cache(maxsize=None)
def _get_engine(db_path: str):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    return engine


def _engine():
    return _get_engine(_resolve_db_path())


def get_engine():
    """Shared SQLModel engine — the same SQLite file backs every domain's
    tables (rotation config, prompt versions, ...), all registered on the
    single `SQLModel.metadata`."""
    return _engine()


def get_rotation_config(tenant_id: str | None = None) -> RotationConfig:
    """Resolve rotation config: tenant-specific row if present, else the
    global default row, else a built-in (not persisted) default."""
    with Session(_engine()) as session:
        if tenant_id is not None:
            row = session.exec(
                select(RotationConfig).where(RotationConfig.tenant_id == tenant_id)
            ).first()
            if row is not None:
                return row
        row = session.exec(
            select(RotationConfig).where(RotationConfig.tenant_id.is_(None))
        ).first()
        if row is not None:
            return row
    return RotationConfig(max_size_mb=50.0, rotation_interval="midnight", backup_count=7)


def set_rotation_config(
    *,
    tenant_id: str | None = None,
    log_file_path: str | None = None,
    max_size_mb: float | None = None,
    rotation_interval: str | None = None,
    backup_count: int = 7,
    compress_backups: bool = False,
) -> RotationConfig:
    """Create or update the rotation config row for `tenant_id` (None = global default)."""
    with Session(_engine()) as session:
        row = session.exec(
            select(RotationConfig).where(RotationConfig.tenant_id == tenant_id)
        ).first()
        if row is None:
            row = RotationConfig(tenant_id=tenant_id)
            session.add(row)
        row.log_file_path = log_file_path
        row.max_size_mb = max_size_mb
        row.rotation_interval = rotation_interval
        row.backup_count = backup_count
        row.compress_backups = compress_backups
        row.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(row)
        return row
