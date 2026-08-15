"""SHA-256 prompt fingerprinting and auto version registration (PRD §8.1, §8.2).

A version is identified by `(slot_name, content_hash)` — hashing the same
template + model params against the same slot always resolves to the same
row, so callers never have to manually bump a version number. The hash is
computed over the *template*, not a rendered instance of it, so re-running
the same template with different variable values links to the same
version rather than minting a new one (PRD §8.1's "dynamic/templated
variation" case).

Titles and promotion aliases are mutable metadata layered on the immutable
content hash (PRD §8.2, feature 2.7): renaming a title or moving
`production`/`canary`/`shadow` never changes which hash a version points
to, so audit identity stays intact. Aliases live in their own
`(slot_name, alias)` mapping rather than an `aliases[]` column on the
version row — an alias points at exactly one version per slot at a time,
and promoting means moving that pointer, not rewriting version history.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, Session, SQLModel, UniqueConstraint, select

from wisetraceloom.config import get_engine

# Documented well-known promotion aliases (PRD §8.2). Custom alias names
# are allowed too — this set is for callers/docs, not an allowlist gate.
PROMOTION_ALIASES = frozenset({"production", "canary", "shadow"})


class PromptVersionError(Exception):
    """Unknown version id, slot mismatch, or invalid alias/title input."""


class PromptVersion(SQLModel, table=True):
    """A registered version of the prompt in logical slot `slot_name`."""

    id: int | None = Field(default=None, primary_key=True)
    slot_name: str = Field(index=True)
    content_hash: str = Field(index=True)
    version_number: int
    title: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PromptAlias(SQLModel, table=True):
    """Mutable promotion pointer: within `slot_name`, `alias` names exactly
    one `PromptVersion` (e.g. `production` → v14). Moving the alias is how
    rollout happens without a redeploy."""

    __table_args__ = (UniqueConstraint("slot_name", "alias", name="uq_prompt_alias_slot_alias"),)

    id: int | None = Field(default=None, primary_key=True)
    slot_name: str = Field(index=True)
    alias: str
    prompt_version_id: int = Field(index=True)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def fingerprint_prompt(template: str, *, model_params: dict[str, Any] | None = None) -> str:
    """SHA-256 hash over the normalized template + model params.

    Normalization only strips formatting noise (surrounding whitespace,
    trailing whitespace per line) so trivial reformatting doesn't register
    as a new version; model params are canonicalized via sorted-key JSON so
    key order never affects the hash.
    """
    normalized_template = "\n".join(line.rstrip() for line in template.strip().splitlines())
    normalized_params = json.dumps(model_params or {}, sort_keys=True, default=str)
    digest_input = f"{normalized_template}\x00{normalized_params}".encode("utf-8")
    return hashlib.sha256(digest_input).hexdigest()


def register_prompt_version(
    slot_name: str,
    template: str,
    *,
    model_params: dict[str, Any] | None = None,
) -> PromptVersion:
    """Auto-detect the version for `template` in `slot_name`: link to the
    existing row with the same content hash, or register a new one with a
    default system-generated title."""
    content_hash = fingerprint_prompt(template, model_params=model_params)

    with Session(get_engine()) as session:
        existing = session.exec(
            select(PromptVersion).where(
                PromptVersion.slot_name == slot_name,
                PromptVersion.content_hash == content_hash,
            )
        ).first()
        if existing is not None:
            return existing

        prior_versions = session.exec(
            select(PromptVersion).where(PromptVersion.slot_name == slot_name)
        ).all()
        version_number = len(prior_versions) + 1
        created_at = datetime.now(timezone.utc)
        title = f"{slot_name} — v{version_number} — {created_at.strftime('%Y-%m-%dT%H:%MZ')}"

        row = PromptVersion(
            slot_name=slot_name,
            content_hash=content_hash,
            version_number=version_number,
            title=title,
            created_at=created_at,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def get_prompt_version(version_id: int) -> PromptVersion:
    """Fetch a registered version by id. Raises `PromptVersionError` if missing."""
    with Session(get_engine()) as session:
        row = session.get(PromptVersion, version_id)
        if row is None:
            raise PromptVersionError(f"unknown prompt version id: {version_id}")
        return row


def set_prompt_title(version_id: int, title: str) -> PromptVersion:
    """Assign a human-readable title. Never changes `content_hash` /
    `version_number` — title is mutable metadata only (PRD §8.2)."""
    title = title.strip()
    if not title:
        raise PromptVersionError("title must be non-empty")

    with Session(get_engine()) as session:
        row = session.get(PromptVersion, version_id)
        if row is None:
            raise PromptVersionError(f"unknown prompt version id: {version_id}")
        row.title = title
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def _normalize_alias(alias: str) -> str:
    """Strip + lowercase so `Production` and `production` are one pointer."""
    return alias.strip().lower()


def set_prompt_alias(slot_name: str, alias: str, version_id: int) -> PromptAlias:
    """Point `alias` (e.g. `production` / `canary` / `shadow`) at `version_id`
    within `slot_name`. Replaces any prior pointer for the same alias — no
    redeploy required to change which version an alias serves."""
    alias = _normalize_alias(alias)
    if not alias:
        raise PromptVersionError("alias must be non-empty")

    with Session(get_engine()) as session:
        version = session.get(PromptVersion, version_id)
        if version is None:
            raise PromptVersionError(f"unknown prompt version id: {version_id}")
        if version.slot_name != slot_name:
            raise PromptVersionError(
                f"version {version_id} belongs to slot {version.slot_name!r}, not {slot_name!r}"
            )

        row = session.exec(
            select(PromptAlias).where(
                PromptAlias.slot_name == slot_name,
                PromptAlias.alias == alias,
            )
        ).first()
        if row is None:
            row = PromptAlias(slot_name=slot_name, alias=alias, prompt_version_id=version_id)
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                # Concurrent first-writer race on (slot_name, alias): the other
                # transaction won the insert — reload and update that row.
                session.rollback()
                row = session.exec(
                    select(PromptAlias).where(
                        PromptAlias.slot_name == slot_name,
                        PromptAlias.alias == alias,
                    )
                ).first()
                if row is None:
                    raise PromptVersionError(
                        f"failed to set alias {alias!r} for slot {slot_name!r} after conflict"
                    )
                row.prompt_version_id = version_id
                row.updated_at = datetime.now(timezone.utc)
                session.commit()
        else:
            row.prompt_version_id = version_id
            row.updated_at = datetime.now(timezone.utc)
            session.commit()
        session.refresh(row)
        return row


def clear_prompt_alias(slot_name: str, alias: str) -> None:
    """Remove the alias pointer for `(slot_name, alias)` if present. No-op
    when unset — clearing is idempotent."""
    alias = _normalize_alias(alias)
    if not alias:
        return
    with Session(get_engine()) as session:
        row = session.exec(
            select(PromptAlias).where(
                PromptAlias.slot_name == slot_name,
                PromptAlias.alias == alias,
            )
        ).first()
        if row is not None:
            session.delete(row)
            session.commit()


def resolve_prompt_alias(slot_name: str, alias: str) -> PromptVersion | None:
    """Return the `PromptVersion` currently named by `alias` in `slot_name`,
    or `None` if the alias is unset."""
    alias = _normalize_alias(alias)
    if not alias:
        return None
    with Session(get_engine()) as session:
        pointer = session.exec(
            select(PromptAlias).where(
                PromptAlias.slot_name == slot_name,
                PromptAlias.alias == alias,
            )
        ).first()
        if pointer is None:
            return None
        version = session.get(PromptVersion, pointer.prompt_version_id)
        if version is None:
            raise PromptVersionError(
                f"alias {alias!r} in slot {slot_name!r} points at missing version "
                f"{pointer.prompt_version_id}"
            )
        return version


def list_prompt_aliases(slot_name: str) -> list[PromptAlias]:
    """All alias pointers currently set for `slot_name`."""
    with Session(get_engine()) as session:
        return list(
            session.exec(select(PromptAlias).where(PromptAlias.slot_name == slot_name)).all()
        )
