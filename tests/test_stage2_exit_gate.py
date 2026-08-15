"""Stage 2 exit gate (feature 2.10): SOC 2 technical-control checklist
plus GDPR Art. 17 erasure with the audit chain intact post-erasure.

PRD Recommendations Stage 2 threshold: "pass SOC 2 controls; demonstrate
GDPR Art. 17 erasure with an intact audit chain."

Product-control gate, not a company SOC 2 Type II attestation — verifies
the Trust Service Criteria Stage 2 already shipped (access, residency,
encryption, fail-closed masking, tamper-evident audit, change management,
availability) still hold when composed, and that shredding a subject's key
does not rewrite or break the hash chain computed over ciphertext.
"""

import pytest
from sqlmodel import Session, select

import wisetraceloom.config as config
import wisetraceloom.instrumentation as instrumentation
from wisetraceloom.audit_chain import anchor_commits, verify_anchor, verify_chain
from wisetraceloom.config import get_engine
from wisetraceloom.crypto_shred import confirm_erasure, decrypt_for_subject, encrypt_for_subject, request_erasure
from wisetraceloom.evaluation import EvalCaseResult, EvalRegressionError, GoldenCase, clear_golden_set, set_golden_set
from wisetraceloom.instrumentation import tool_call
from wisetraceloom.logging import configure
from wisetraceloom.masking import MaskingError, set_masking_callback
from wisetraceloom.prompts import register_prompt_version, resolve_prompt_alias, set_prompt_alias
from wisetraceloom.residency import UnroutedRegionError, set_region_config
from wisetraceloom.storage import StorageCommit, append_commit, read_as_of_version, read_latest
from wisetraceloom.tenancy import AccessDeniedError, create_tenant, grant_role, isolated_stream_id, query_latest

PLAINTEXT_PII = "alice@example.com"
SUBJECT_ID = "alice"
SLOT = "router_agent.system_prompt"


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_db_path_override", str(tmp_path / "cfg.db"))


@pytest.fixture(autouse=True)
def _restore_process_locals():
    yield
    set_masking_callback(None)
    clear_golden_set()


def _entry_hashes(stream_id: str) -> list[str]:
    with Session(get_engine()) as session:
        rows = session.exec(
            select(StorageCommit).where(StorageCommit.stream_id == stream_id).order_by(StorageCommit.version)
        ).all()
    return [row.entry_hash for row in rows]


def _mutate_payload(stream_id: str, version: int, payload: str) -> None:
    with Session(get_engine()) as session:
        row = session.exec(
            select(StorageCommit).where(StorageCommit.stream_id == stream_id, StorageCommit.version == version)
        ).one()
        row.payload = payload
        session.add(row)
        session.commit()


def test_cc6_1_viewer_rbac_isolates_tenant_data():
    create_tenant("acme")
    create_tenant("globex")
    append_commit(isolated_stream_id("spans", "acme"), "span", {"msg": "acme-only"}, tenant_id="acme")
    append_commit(isolated_stream_id("spans", "globex"), "span", {"msg": "globex-only"}, tenant_id="globex")
    grant_role("bob", "acme", "viewer")

    with pytest.raises(AccessDeniedError):
        query_latest("eve", "acme")
    with pytest.raises(AccessDeniedError):
        query_latest("bob", "globex")

    rows = query_latest("bob", "acme")
    assert rows == [{"msg": "acme-only"}]


def test_cc6_3_unrouted_region_fails_closed():
    set_region_config(tenant_id="regulated", region="ap-south-1")
    with pytest.raises(UnroutedRegionError):
        append_commit("spans", "span", {"msg": "must-not-land"}, tenant_id="regulated")
    assert read_latest("spans") == []


def test_cc6_6_and_p4_2_gdpr_art17_erasure_leaves_audit_chain_intact():
    ciphertext = encrypt_for_subject(SUBJECT_ID, PLAINTEXT_PII)
    append_commit(
        "subject_records",
        "profile",
        {"subject_id": SUBJECT_ID, "encrypted_email": ciphertext, "note": "ticket"},
    )
    append_commit(
        "subject_records",
        "profile",
        {"subject_id": SUBJECT_ID, "encrypted_email": ciphertext, "note": "follow-up"},
    )

    assert decrypt_for_subject(SUBJECT_ID, ciphertext) == PLAINTEXT_PII
    assert verify_chain("subject_records").ok is True

    record = anchor_commits("subject_records", lambda stream_id, root: f"ext:{stream_id}:{root[:8]}")
    assert verify_anchor(record) is True
    hashes_before = _entry_hashes("subject_records")

    stored = read_latest("subject_records")
    assert len(stored) == 2
    assert PLAINTEXT_PII not in str(stored)
    assert stored[0]["encrypted_email"] == ciphertext

    request = request_erasure(SUBJECT_ID, requested_by="dpo@acme.com", scope="all")
    confirmed = confirm_erasure(request.id)
    assert confirmed.status == "Confirmed"
    assert decrypt_for_subject(SUBJECT_ID, ciphertext) is None

    after = read_latest("subject_records")
    assert after[0]["encrypted_email"] == ciphertext
    assert PLAINTEXT_PII not in str(after)
    assert read_as_of_version("subject_records", 1)[0]["encrypted_email"] == ciphertext
    assert _entry_hashes("subject_records") == hashes_before
    assert verify_chain("subject_records").ok is True
    assert verify_anchor(record) is True

    facts = read_latest("erasure_log")
    assert len(facts) == 1
    assert facts[0]["subject_id"] == SUBJECT_ID
    assert facts[0]["key_generations_destroyed"] == 1
    assert PLAINTEXT_PII not in str(facts)
    assert "dpo@acme.com" not in str(facts)
    assert verify_chain("erasure_log").ok is True


def test_cc6_7_masking_failure_blocks_unmasked_storage():
    def broken(payload):
        raise ValueError("masking blew up")

    set_masking_callback(broken)
    with pytest.raises(MaskingError):
        append_commit("spans", "event", {"password": "hunter2"})

    with Session(get_engine()) as session:
        rows = session.exec(select(StorageCommit).where(StorageCommit.stream_id == "spans")).all()
    assert rows == []


def test_cc7_2_tamper_breaks_hash_chain():
    append_commit("audit", "event", {"n": 1})
    append_commit("audit", "event", {"n": 2})
    assert verify_chain("audit").ok is True

    _mutate_payload("audit", 2, '{"n": 999}')
    result = verify_chain("audit")
    assert result.ok is False
    assert result.broken_at_version == 2


def test_cc8_1_production_promotion_blocked_on_eval_regression():
    cases = [GoldenCase(case_id=f"c{i}") for i in range(10)]

    def runner_pass(template, case):
        return EvalCaseResult(passed=True, cost_usd=1.0, latency_ms=100.0)

    def runner_fail(template, case):
        idx = int(case.case_id[1:])
        return EvalCaseResult(passed=idx < 9, cost_usd=1.0, latency_ms=100.0)

    set_golden_set(SLOT, cases, runner_pass)
    v1 = register_prompt_version(SLOT, "v1")
    set_prompt_alias(SLOT, "production", v1.id)

    set_golden_set(SLOT, cases, runner_fail)
    v2 = register_prompt_version(SLOT, "v2")
    with pytest.raises(EvalRegressionError, match="pass rate"):
        set_prompt_alias(SLOT, "production", v2.id)
    assert resolve_prompt_alias(SLOT, "production").id == v1.id


def test_a1_2_broken_storage_never_propagates_to_host(monkeypatch):
    configure()

    def broken_enqueue(*args, **kwargs):
        raise RuntimeError("storage down")

    monkeypatch.setattr(instrumentation, "enqueue_append", broken_enqueue)
    with tool_call("search"):
        pass
