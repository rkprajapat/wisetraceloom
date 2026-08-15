import pytest

import wisetraceloom.config as config
from wisetraceloom.prompts import (
    PROMOTION_ALIASES,
    PromptVersionError,
    clear_prompt_alias,
    fingerprint_prompt,
    get_prompt_version,
    list_prompt_aliases,
    register_prompt_version,
    resolve_prompt_alias,
    set_prompt_alias,
    set_prompt_title,
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    # Own SQLite file per test — the process-wide engine cache (keyed by
    # path) never leaks state between tests.
    monkeypatch.setattr(config, "_db_path_override", str(tmp_path / "test.db"))


def test_fingerprint_stable_for_same_template_and_params():
    a = fingerprint_prompt("You are a helpful agent.", model_params={"temperature": 0.2})
    b = fingerprint_prompt("You are a helpful agent.", model_params={"temperature": 0.2})
    assert a == b


def test_fingerprint_changes_when_template_changes():
    a = fingerprint_prompt("You are a helpful agent.")
    b = fingerprint_prompt("You are a stricter agent.")
    assert a != b


def test_fingerprint_ignores_trailing_whitespace_noise():
    a = fingerprint_prompt("line one\nline two")
    b = fingerprint_prompt("line one   \nline two\n")
    assert a == b


def test_fingerprint_ignores_model_param_key_order():
    a = fingerprint_prompt("prompt", model_params={"temperature": 0.2, "max_tokens": 100})
    b = fingerprint_prompt("prompt", model_params={"max_tokens": 100, "temperature": 0.2})
    assert a == b


def test_register_new_version_gets_default_title_and_version_number_one():
    version = register_prompt_version("router_agent.system_prompt", "You are a router.")
    assert version.version_number == 1
    assert version.slot_name == "router_agent.system_prompt"
    assert "router_agent.system_prompt" in version.title
    assert "v1" in version.title


def test_repeat_hash_links_to_existing_version_instead_of_duplicating():
    first = register_prompt_version("router_agent.system_prompt", "You are a router.")
    second = register_prompt_version("router_agent.system_prompt", "You are a router.")
    assert first.id == second.id
    assert second.version_number == 1


def test_changed_template_registers_new_incremented_version():
    v1 = register_prompt_version("router_agent.system_prompt", "You are a router.")
    v2 = register_prompt_version("router_agent.system_prompt", "You are a stricter router.")
    assert v2.id != v1.id
    assert v2.version_number == 2


def test_dynamic_variable_substitution_does_not_mint_new_version():
    # Same template, no re-substitution happens before hashing — callers
    # pass the template itself, so different runtime variable values never
    # reach fingerprint_prompt as different text.
    v1 = register_prompt_version("greeter.system_prompt", "Hello {name}, welcome.")
    v2 = register_prompt_version("greeter.system_prompt", "Hello {name}, welcome.")
    assert v1.id == v2.id


def test_different_slots_get_independent_version_numbering():
    router_v1 = register_prompt_version("router_agent.system_prompt", "A")
    greeter_v1 = register_prompt_version("greeter.system_prompt", "B")
    assert router_v1.version_number == 1
    assert greeter_v1.version_number == 1
    assert router_v1.slot_name != greeter_v1.slot_name


# --- feature 2.7: human titles + promotion aliases ---


def test_set_prompt_title_updates_title_without_changing_identity():
    version = register_prompt_version("router_agent.system_prompt", "You are a router.")
    original_hash = version.content_hash
    original_number = version.version_number

    updated = set_prompt_title(version.id, "Stricter tool-selection guardrail")
    assert updated.title == "Stricter tool-selection guardrail"
    assert updated.content_hash == original_hash
    assert updated.version_number == original_number
    assert get_prompt_version(version.id).title == "Stricter tool-selection guardrail"


def test_set_prompt_title_rejects_empty_and_unknown_id():
    version = register_prompt_version("router_agent.system_prompt", "You are a router.")
    with pytest.raises(PromptVersionError, match="non-empty"):
        set_prompt_title(version.id, "   ")
    with pytest.raises(PromptVersionError, match="unknown"):
        set_prompt_title(999_999, "ghost")


def test_set_prompt_alias_points_production_canary_shadow_without_redeploy():
    assert PROMOTION_ALIASES == frozenset({"production", "canary", "shadow"})
    v1 = register_prompt_version("router_agent.system_prompt", "v1 template")
    v2 = register_prompt_version("router_agent.system_prompt", "v2 template")

    set_prompt_alias("router_agent.system_prompt", "production", v1.id)
    set_prompt_alias("router_agent.system_prompt", "canary", v2.id)
    set_prompt_alias("router_agent.system_prompt", "shadow", v2.id)

    assert resolve_prompt_alias("router_agent.system_prompt", "production").id == v1.id
    assert resolve_prompt_alias("router_agent.system_prompt", "canary").id == v2.id
    assert resolve_prompt_alias("router_agent.system_prompt", "shadow").id == v2.id

    # Promote: move production pointer to v2 — no code deploy, just metadata.
    set_prompt_alias("router_agent.system_prompt", "production", v2.id)
    assert resolve_prompt_alias("router_agent.system_prompt", "production").id == v2.id


def test_set_prompt_alias_rejects_slot_mismatch_and_unknown_version():
    router = register_prompt_version("router_agent.system_prompt", "router")
    greeter = register_prompt_version("greeter.system_prompt", "greeter")

    with pytest.raises(PromptVersionError, match="belongs to slot"):
        set_prompt_alias("router_agent.system_prompt", "production", greeter.id)
    with pytest.raises(PromptVersionError, match="unknown"):
        set_prompt_alias("router_agent.system_prompt", "production", 999_999)
    with pytest.raises(PromptVersionError, match="non-empty"):
        set_prompt_alias("router_agent.system_prompt", "  ", router.id)


def test_clear_prompt_alias_is_idempotent():
    v1 = register_prompt_version("router_agent.system_prompt", "v1")
    set_prompt_alias("router_agent.system_prompt", "canary", v1.id)
    clear_prompt_alias("router_agent.system_prompt", "canary")
    assert resolve_prompt_alias("router_agent.system_prompt", "canary") is None
    clear_prompt_alias("router_agent.system_prompt", "canary")  # no-op
    assert list_prompt_aliases("router_agent.system_prompt") == []


def test_aliases_are_isolated_per_slot():
    a = register_prompt_version("slot.a", "A")
    b = register_prompt_version("slot.b", "B")
    set_prompt_alias("slot.a", "production", a.id)
    set_prompt_alias("slot.b", "production", b.id)
    assert resolve_prompt_alias("slot.a", "production").id == a.id
    assert resolve_prompt_alias("slot.b", "production").id == b.id
    assert len(list_prompt_aliases("slot.a")) == 1


def test_alias_names_are_case_insensitive():
    v1 = register_prompt_version("router_agent.system_prompt", "v1")
    set_prompt_alias("router_agent.system_prompt", "Production", v1.id)
    assert resolve_prompt_alias("router_agent.system_prompt", "PRODUCTION").id == v1.id
    clear_prompt_alias("router_agent.system_prompt", "production")
    assert resolve_prompt_alias("router_agent.system_prompt", "Production") is None


def test_resolve_raises_when_alias_points_at_missing_version():
    from sqlmodel import Session

    from wisetraceloom.config import get_engine
    from wisetraceloom.prompts import PromptVersion

    v1 = register_prompt_version("router_agent.system_prompt", "v1")
    set_prompt_alias("router_agent.system_prompt", "canary", v1.id)
    with Session(get_engine()) as session:
        row = session.get(PromptVersion, v1.id)
        session.delete(row)
        session.commit()
    with pytest.raises(PromptVersionError, match="missing version"):
        resolve_prompt_alias("router_agent.system_prompt", "canary")
