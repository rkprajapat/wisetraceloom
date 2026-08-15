import pytest

import wisetraceloom.config as config
from wisetraceloom.instrumentation import tool_call
from wisetraceloom.storage import append_commit, read_latest, wait_for_pending_writes
from wisetraceloom.tenancy import (
    DEFAULT_NAMESPACE,
    AccessDeniedError,
    TenancyError,
    assert_viewer_access,
    create_namespace,
    create_tenant,
    grant_role,
    isolated_stream_id,
    list_namespaces,
    query_latest,
    resolve_role,
    revoke_role,
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_db_path_override", str(tmp_path / "test.db"))


def test_create_tenant_mints_default_namespace():
    create_tenant("acme", display_name="Acme Corp")
    names = {ns.name for ns in list_namespaces("acme")}
    assert names == {DEFAULT_NAMESPACE}


def test_duplicate_tenant_rejected():
    create_tenant("acme")
    with pytest.raises(TenancyError, match="already exists"):
        create_tenant("acme")


def test_create_namespace_isolated_per_tenant():
    create_tenant("acme")
    create_tenant("other")
    create_namespace("acme", "staging")
    acme = {ns.name for ns in list_namespaces("acme")}
    other = {ns.name for ns in list_namespaces("other")}
    assert "staging" in acme
    assert "staging" not in other


def test_namespace_rejects_colon_and_reserved_star():
    create_tenant("acme")
    with pytest.raises(TenancyError, match=":"):
        create_namespace("acme", "foo:bar")
    with pytest.raises(TenancyError, match="reserved"):
        create_namespace("acme", "*")
    with pytest.raises(TenancyError, match="non-empty"):
        create_tenant("  ")


def test_grant_and_resolve_role():
    create_tenant("acme", owner_principal_id="alice")
    assert resolve_role("alice", "acme") == "owner"
    grant_role("bob", "acme", "viewer")
    assert resolve_role("bob", "acme") == "viewer"
    assert resolve_role("carol", "acme") is None


def test_tenant_wide_role_covers_named_namespace():
    create_tenant("acme")
    create_namespace("acme", "staging")
    grant_role("alice", "acme", "admin")
    assert resolve_role("alice", "acme", "staging") == "admin"
    assert resolve_role("alice", "acme", DEFAULT_NAMESPACE) == "admin"


def test_namespace_scoped_role_does_not_cover_other_namespace():
    create_tenant("acme")
    create_namespace("acme", "staging")
    grant_role("bob", "acme", "viewer", namespace="staging")
    assert resolve_role("bob", "acme", "staging") == "viewer"
    assert resolve_role("bob", "acme", DEFAULT_NAMESPACE) is None
    with pytest.raises(AccessDeniedError):
        assert_viewer_access("bob", "acme")


def test_tenant_wide_owner_not_downgraded_by_namespace_viewer_grant():
    create_tenant("acme", owner_principal_id="alice")
    create_namespace("acme", "staging")
    grant_role("alice", "acme", "viewer", namespace="staging")
    assert resolve_role("alice", "acme", "staging") == "owner"


def test_unknown_role_rejected():
    create_tenant("acme")
    with pytest.raises(TenancyError, match="role must be one of"):
        grant_role("alice", "acme", "superuser")


def test_revoke_then_deny():
    create_tenant("acme")
    grant_role("bob", "acme", "viewer")
    assert_viewer_access("bob", "acme")
    revoke_role("bob", "acme")
    with pytest.raises(AccessDeniedError):
        assert_viewer_access("bob", "acme")
    revoke_role("bob", "acme")  # idempotent


def test_query_latest_denies_without_membership():
    create_tenant("acme")
    append_commit(isolated_stream_id("spans", "acme"), "event", {"n": 1}, tenant_id="acme")
    with pytest.raises(AccessDeniedError):
        query_latest("eve", "acme")


def test_query_latest_isolates_tenants_at_storage_and_query_layer():
    create_tenant("acme", owner_principal_id="alice")
    create_tenant("other", owner_principal_id="olivia")
    append_commit(isolated_stream_id("spans", "acme"), "event", {"n": "acme-only"}, tenant_id="acme")
    append_commit(isolated_stream_id("spans", "other"), "event", {"n": "other-only"}, tenant_id="other")

    acme_rows = query_latest("alice", "acme")
    other_rows = query_latest("olivia", "other")
    assert [row["n"] for row in acme_rows] == ["acme-only"]
    assert [row["n"] for row in other_rows] == ["other-only"]

    # Ungated read of the other tenant's isolated stream is a different
    # stream_id — alice's stream never contained olivia's payload.
    assert all(row.get("n") != "other-only" for row in read_latest(isolated_stream_id("spans", "acme")))
    with pytest.raises(AccessDeniedError):
        query_latest("alice", "other")


def test_query_latest_namespace_partition():
    create_tenant("acme", owner_principal_id="alice")
    create_namespace("acme", "staging")
    grant_role("bob", "acme", "viewer", namespace="staging")
    append_commit(
        isolated_stream_id("spans", "acme", "staging"), "event", {"n": "staging"}, tenant_id="acme"
    )
    append_commit(isolated_stream_id("spans", "acme"), "event", {"n": "default"}, tenant_id="acme")

    assert [row["n"] for row in query_latest("bob", "acme", namespace="staging")] == ["staging"]
    with pytest.raises(AccessDeniedError):
        query_latest("bob", "acme")
    assert [row["n"] for row in query_latest("alice", "acme")] == ["default"]


def test_tool_call_persists_to_isolated_stream_not_shared_spans():
    with tool_call("search", tenant_id="acme"):
        pass
    wait_for_pending_writes()
    isolated = read_latest(isolated_stream_id("spans", "acme"), tenant_id="acme")
    shared = read_latest("spans")
    assert any(row.get("tool_name") == "search" for row in isolated)
    assert not any(row.get("tool_name") == "search" for row in shared)


def test_isolated_stream_id_shape():
    assert isolated_stream_id("spans", "acme") == "spans:acme:default"
    assert isolated_stream_id("spans", "acme", "staging") == "spans:acme:staging"
    with pytest.raises(TenancyError, match=":"):
        isolated_stream_id("spans", "acme:prod")
