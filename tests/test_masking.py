import pytest
from sqlmodel import Session, select

import wisetraceloom.config as config
from wisetraceloom.config import get_engine
from wisetraceloom.masking import (
    MaskingError,
    apply_masking,
    default_masking_callback,
    get_masking_callback,
    set_masking_callback,
)
from wisetraceloom.storage import StorageCommit, append_commit


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_db_path_override", str(tmp_path / "test.db"))


@pytest.fixture(autouse=True)
def _restore_default_callback():
    # Global registry (module-level `_masking_callback`) must not leak a
    # test-installed callback into other test files/modules.
    yield
    set_masking_callback(None)


def test_default_callback_redacts_structured_field_by_key():
    payload = {"password": "hunter2", "count": 3}
    masked = default_masking_callback(payload)
    assert masked["password"] == "[REDACTED]"
    assert masked["count"] == 3


def test_default_callback_redacts_regex_matches_in_remaining_strings():
    payload = {"note": "reach me at jane@example.com"}
    masked = default_masking_callback(payload)
    assert masked["note"] == "reach me at [REDACTED_EMAIL]"


def test_default_callback_applies_structured_before_regex():
    # "email" is both a structured field name and would otherwise match the
    # regex tier — structured redaction must win, exactly like feature 1.4's
    # own processor ordering.
    payload = {"email": "jane@example.com"}
    masked = default_masking_callback(payload)
    assert masked["email"] == "[REDACTED]"


def test_apply_masking_uses_default_callback_when_none_registered():
    masked = apply_masking({"password": "hunter2", "plan": "pro"})
    assert masked["password"] == "[REDACTED]"
    assert masked["plan"] == "pro"


def test_set_masking_callback_registers_custom_callback():
    def custom(payload):
        return {**payload, "custom": True}

    set_masking_callback(custom)
    assert get_masking_callback() is custom
    assert apply_masking({"a": 1}) == {"a": 1, "custom": True}


def test_set_masking_callback_none_resets_to_default():
    set_masking_callback(lambda payload: {})
    set_masking_callback(None)
    assert get_masking_callback() is default_masking_callback


def test_apply_masking_wraps_callback_exception_in_masking_error():
    def broken(payload):
        raise ValueError("boom")

    set_masking_callback(broken)
    with pytest.raises(MaskingError, match="boom"):
        apply_masking({"a": 1})


def test_apply_masking_rejects_non_dict_return_value():
    set_masking_callback(lambda payload: "not a dict")
    with pytest.raises(MaskingError, match="dict"):
        apply_masking({"a": 1})


def test_append_commit_masks_payload_before_persisting():
    commit = append_commit("stream-a", "event", {"password": "hunter2", "plan": "pro"})
    assert "hunter2" not in commit.payload
    assert '"password": "[REDACTED]"' in commit.payload or '"password":"[REDACTED]"' in commit.payload
    assert "pro" in commit.payload


def test_append_commit_uses_custom_registered_callback():
    set_masking_callback(lambda payload: {**payload, "masked_by": "custom"})
    commit = append_commit("stream-a", "event", {"n": 1})
    assert '"masked_by": "custom"' in commit.payload


def test_append_commit_blocks_write_when_masking_fails():
    # Fail-closed (PRD §3, §7, feature 2.6): a masking failure must prevent
    # the write entirely, distinct from feature 1.5's fail-open instrumentation
    # posture — no row should land in storage, masked or not.
    def broken(payload):
        raise ValueError("masking blew up")

    set_masking_callback(broken)
    with pytest.raises(MaskingError):
        append_commit("stream-a", "event", {"password": "hunter2"})

    with Session(get_engine()) as session:
        rows = session.exec(select(StorageCommit).where(StorageCommit.stream_id == "stream-a")).all()
    assert rows == []
