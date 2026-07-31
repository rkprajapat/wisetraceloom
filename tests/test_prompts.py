import pytest

import trailwise.config as config
from trailwise.prompts import fingerprint_prompt, register_prompt_version


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
