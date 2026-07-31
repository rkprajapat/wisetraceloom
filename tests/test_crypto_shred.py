import pytest

import wisetraceloom.config as config
from wisetraceloom.crypto_shred import (
    ErasureRequestError,
    confirm_erasure,
    decrypt_for_subject,
    encrypt_for_subject,
    get_active_subject_key,
    get_or_create_subject_key,
    request_erasure,
)
from wisetraceloom.storage import read_latest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_db_path_override", str(tmp_path / "test.db"))


def test_encrypt_then_decrypt_roundtrips():
    token = encrypt_for_subject("alice", "alice@example.com")
    assert decrypt_for_subject("alice", token) == "alice@example.com"


def test_get_or_create_subject_key_provisions_once_and_is_stable():
    first = get_or_create_subject_key("alice")
    second = get_or_create_subject_key("alice")
    assert first.id == second.id
    assert first.generation == 1


def test_different_subjects_get_different_keys():
    alice_key = get_or_create_subject_key("alice")
    bob_key = get_or_create_subject_key("bob")
    assert alice_key.key_material != bob_key.key_material


def test_ciphertext_from_one_subject_does_not_decrypt_as_another():
    token = encrypt_for_subject("alice", "secret")
    assert decrypt_for_subject("bob", token) is None


def test_decrypt_unknown_subject_returns_none():
    assert decrypt_for_subject("nobody", "1.gAAAAA") is None


def test_erasure_workflow_starts_requested():
    request = request_erasure("alice", requested_by="alice@example.com", scope="all")
    assert request.status == "Requested"
    assert request.confirmed_at is None
    # Requesting alone must not touch key material.
    encrypt_for_subject("alice", "secret")
    assert get_active_subject_key("alice") is not None


def test_confirm_erasure_destroys_active_key_and_marks_confirmed():
    encrypt_for_subject("alice", "secret")
    request = request_erasure("alice")

    confirmed = confirm_erasure(request.id)

    assert confirmed.status == "Confirmed"
    assert confirmed.confirmed_at is not None
    assert get_active_subject_key("alice") is None


def test_confirm_erasure_renders_prior_ciphertext_unrecoverable():
    token = encrypt_for_subject("alice", "alice@example.com")
    request = request_erasure("alice")
    confirm_erasure(request.id)

    assert decrypt_for_subject("alice", token) is None


def test_confirm_erasure_unknown_request_raises():
    with pytest.raises(ErasureRequestError):
        confirm_erasure(999999)


def test_confirm_erasure_twice_raises():
    request = request_erasure("alice")
    confirm_erasure(request.id)
    with pytest.raises(ErasureRequestError):
        confirm_erasure(request.id)


def test_confirm_erasure_appends_erasure_fact_without_pii():
    encrypt_for_subject("alice", "alice@example.com")
    request = request_erasure("alice", requested_by="dpo@acme.com", scope="all")
    confirm_erasure(request.id)

    facts = read_latest("erasure_log")
    assert len(facts) == 1
    fact = facts[0]
    assert fact["subject_id"] == "alice"
    assert fact["erasure_request_id"] == request.id
    assert fact["requested_by"] == "dpo@acme.com"
    assert fact["key_generations_destroyed"] == 1
    serialized = str(fact)
    assert "alice@example.com" not in serialized


def test_subject_can_re_provision_key_after_erasure():
    encrypt_for_subject("alice", "old-data")
    request = request_erasure("alice")
    confirm_erasure(request.id)

    new_token = encrypt_for_subject("alice", "new-data")
    assert decrypt_for_subject("alice", new_token) == "new-data"

    new_key = get_active_subject_key("alice")
    assert new_key.generation == 2


def test_old_ciphertext_still_unrecoverable_after_key_reprovisioned():
    old_token = encrypt_for_subject("alice", "old-secret")
    request = request_erasure("alice")
    confirm_erasure(request.id)

    encrypt_for_subject("alice", "new-secret")

    assert decrypt_for_subject("alice", old_token) is None
