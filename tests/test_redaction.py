import json

import pytest

import trailwise.config as config
from trailwise.logging import configure, get_logger
from trailwise.redaction import (
    pii_redaction_processor,
    presidio_available,
    redact_regex_matches,
    redact_structured_fields,
    redact_with_presidio,
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_db_path_override", str(tmp_path / "cfg.db"))


def test_structured_redaction_replaces_sensitive_keys_regardless_of_type():
    event = {"password": "hunter2", "attempts": 3, "user": "alice"}
    redacted = redact_structured_fields(event)
    assert redacted["password"] == "[REDACTED]"
    assert redacted["attempts"] == 3
    assert redacted["user"] == "alice"


def test_structured_redaction_is_case_insensitive_on_key():
    event = {"API_KEY": "sk-live-abc123"}
    redacted = redact_structured_fields(event)
    assert redacted["API_KEY"] == "[REDACTED]"


def test_regex_redacts_email():
    assert redact_regex_matches("contact john@example.com now") == "contact [REDACTED_EMAIL] now"


def test_regex_redacts_credit_card_like_digit_run():
    assert redact_regex_matches("card 4111 1111 1111 1111 on file") == "card [REDACTED_CARD] on file"


def test_regex_redacts_phone_number():
    assert redact_regex_matches("call 415-555-0132 today") == "call [REDACTED_PHONE] today"


def test_regex_leaves_clean_text_untouched():
    assert redact_regex_matches("no pii in this sentence") == "no pii in this sentence"


def test_pii_redaction_processor_applies_structured_then_regex():
    event_dict = {
        "event": "user signed up with john@example.com",
        "password": "hunter2",
        "count": 1,
    }
    result = pii_redaction_processor(None, "info", event_dict)
    assert result["event"] == "user signed up with [REDACTED_EMAIL]"
    assert result["password"] == "[REDACTED]"
    assert result["count"] == 1


def test_no_raw_pii_reaches_emitted_log_file(tmp_path):
    log_file = tmp_path / "app.log"
    try:
        configure(file_path=str(log_file))
        logger = get_logger("test.redaction.pipeline")

        logger.info(
            "signup",
            email="jane.doe@example.com",
            phone="415-555-0199",
            card_number="4111-1111-1111-1111",
            password="hunter2",
            plan="pro",
        )

        raw_contents = log_file.read_text()
        assert "jane.doe@example.com" not in raw_contents
        assert "415-555-0199" not in raw_contents
        assert "4111-1111-1111-1111" not in raw_contents
        assert "hunter2" not in raw_contents

        record = json.loads(raw_contents.strip())
        assert record["plan"] == "pro"
        assert record["card_number"] == "[REDACTED]"
        assert record["password"] == "[REDACTED]"
    finally:
        configure()


@pytest.mark.skipif(not presidio_available(), reason="presidio extra not installed")
def test_presidio_redacts_free_text_entities_missed_by_structured_and_regex():
    text = "My name is John Smith and I live in Berlin."
    redacted = redact_with_presidio(text)
    assert "John Smith" not in redacted
    assert "<PERSON>" in redacted
