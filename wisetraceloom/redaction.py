"""Client-side PII redaction (PRD §3), tiered per the approaches-ranked list:

1. **Structured field-name redaction** (cheapest, most precise) — a value is
   redacted whenever its *key* matches a known-sensitive field name,
   regardless of type. Active by default via `pii_redaction_processor`.
2. **Regex pattern scrubbing** (emails, phone numbers, card-like digit
   runs) over remaining string values. Also active by default.
3. **Presidio NER layer**, for free text the first two tiers miss. Optional
   — needs the `presidio` extra (`presidio-analyzer` + `presidio-anonymizer`
   + a spaCy model) installed; `presidio_available()` reports whether it
   is, so callers can compose it into their own pipeline without a hard
   dependency on it being present. Which spaCy model to load is a per-tenant
   `RedactionConfig` row (SQLModel, same store as `wisetraceloom.config`), not
   an environment variable — set it via `set_redaction_config(...)`.

Presidio explicitly does not guarantee finding all PII (PRD §3) — this is
one layer in defense-in-depth, not a substitute for tiers 1-2.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from sqlmodel import Field, Session, SQLModel, select

from wisetraceloom.config import get_engine

SENSITIVE_FIELD_NAMES = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "auth_token",
        "token",
        "authorization",
        "ssn",
        "social_security_number",
        "credit_card",
        "card_number",
        "cvv",
        "email",
        "phone",
        "phone_number",
        "address",
        "dob",
        "date_of_birth",
        "private_key",
    }
)

REDACTED_VALUE = "[REDACTED]"

# Order matters: card-like digit runs are checked before the looser phone
# pattern so an unspaced 16-digit card number is tagged as a card, not
# misread as part of a phone number.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_CREDIT_CARD_RE = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
_PHONE_RE = re.compile(r"(?:\+?\d{1,3}[-.\s])?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")

DEFAULT_PRESIDIO_SPACY_MODEL = "en_core_web_sm"


class RedactionConfig(SQLModel, table=True):
    """Which spaCy model backs the optional Presidio NER layer, per tenant."""

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: str | None = Field(default=None, index=True)
    presidio_spacy_model: str = DEFAULT_PRESIDIO_SPACY_MODEL
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def get_redaction_config(tenant_id: str | None = None) -> RedactionConfig:
    """Resolve redaction config: tenant-specific row if present, else the
    global default row, else a built-in default."""
    with Session(get_engine()) as session:
        if tenant_id is not None:
            row = session.exec(
                select(RedactionConfig).where(RedactionConfig.tenant_id == tenant_id)
            ).first()
            if row is not None:
                return row
        row = session.exec(select(RedactionConfig).where(RedactionConfig.tenant_id.is_(None))).first()
        if row is not None:
            return row
    return RedactionConfig()


def set_redaction_config(*, tenant_id: str | None = None, presidio_spacy_model: str) -> RedactionConfig:
    """Create or update the redaction config row for `tenant_id` (None = global default)."""
    with Session(get_engine()) as session:
        row = session.exec(select(RedactionConfig).where(RedactionConfig.tenant_id == tenant_id)).first()
        if row is None:
            row = RedactionConfig(tenant_id=tenant_id)
            session.add(row)
        row.presidio_spacy_model = presidio_spacy_model
        row.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(row)
        return row


def _resolve_spacy_model(tenant_id: str | None = None) -> str:
    return get_redaction_config(tenant_id=tenant_id).presidio_spacy_model


def redact_structured_fields(
    event_dict: dict[str, Any], *, sensitive_fields: frozenset[str] = SENSITIVE_FIELD_NAMES
) -> dict[str, Any]:
    """Redact every value whose key (case-insensitive) is in `sensitive_fields`."""
    return {
        key: REDACTED_VALUE if key.lower() in sensitive_fields else value
        for key, value in event_dict.items()
    }


def redact_regex_matches(text: str) -> str:
    """Scrub emails, card-like digit runs, and phone numbers out of `text`."""
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = _CREDIT_CARD_RE.sub("[REDACTED_CARD]", text)
    text = _PHONE_RE.sub("[REDACTED_PHONE]", text)
    return text


def pii_redaction_processor(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: structured field-name redaction, then regex
    scrubbing over the remaining string values (including `event` itself)."""
    event_dict = redact_structured_fields(event_dict)
    for key, value in event_dict.items():
        if isinstance(value, str):
            event_dict[key] = redact_regex_matches(value)
    return event_dict


@lru_cache(maxsize=8)
def _presidio_engines_for_model(model_name: str) -> tuple[Any, Any]:
    # Cached per model name (not per tenant) so tenants sharing a model
    # share one loaded spaCy pipeline instead of reloading it per tenant.
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_anonymizer import AnonymizerEngine

    nlp_engine = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": model_name}],
        }
    ).create_engine()
    analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])
    anonymizer = AnonymizerEngine()
    return analyzer, anonymizer


def presidio_available(tenant_id: str | None = None) -> bool:
    """Whether the `presidio` extra (analyzer + anonymizer + spaCy model) is usable."""
    try:
        _presidio_engines_for_model(_resolve_spacy_model(tenant_id=tenant_id))
    except Exception:
        return False
    return True


def redact_with_presidio(text: str, *, tenant_id: str | None = None, language: str = "en") -> str:
    """Run Presidio's Analyzer + Anonymizer over `text`. Raises whatever
    import/model-loading error `presidio_available()` would have caught —
    check that first if the extra may not be installed."""
    analyzer, anonymizer = _presidio_engines_for_model(_resolve_spacy_model(tenant_id=tenant_id))
    results = analyzer.analyze(text=text, language=language)
    return anonymizer.anonymize(text=text, analyzer_results=results).text
