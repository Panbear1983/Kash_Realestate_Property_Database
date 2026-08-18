#!/usr/bin/env python3
"""Admin actions are thin, role-checked wrappers around the audited workflow."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.contribution_admin import ContributionAdmin  # noqa: E402
from kash.contributor_autosync import WorkspaceAutoSync  # noqa: E402
from kash.contributor_sync_audit import WorkspaceSyncAuditReadModel  # noqa: E402
from kash.contributions import ContributionService  # noqa: E402
from kash.data_roles import DataRoles, ROLE_CONTRIBUTOR, ROLE_PUBLISHER, ROLE_REVIEWER  # noqa: E402
from kash.review_queue import ReviewQueue  # noqa: E402
from kash.store import Store  # noqa: E402

OWNER, CONTRIBUTOR, REVIEWER, PUBLISHER = 1, 2, 3, 4


def test_admin_surface_enforces_review_then_publish_and_returns_safe_queue_data():
    store = Store(":memory:")
    roles = DataRoles(store); roles.bootstrap_owner(OWNER)
    for user, role in [(CONTRIBUTOR, ROLE_CONTRIBUTOR), (REVIEWER, ROLE_REVIEWER), (PUBLISHER, ROLE_PUBLISHER)]:
        roles.grant(OWNER, user, role, reason="approved")
    workflow = ContributionService(store, roles)
    proposal = workflow.submit_add(CONTRIBUTOR, {"street_address": "10 Example Road", "zip": "10301"},
                                   source_url="https://example.com/10", observed_at="2026-08-06")
    admin = ContributionAdmin(workflow, ReviewQueue(store, roles))
    assert admin.list(REVIEWER, status="pending")[0]["id"] == proposal["id"]
    assert admin.approve(REVIEWER, proposal["id"], reason="source verified")["status"] == "approved"
    assert admin.publish(PUBLISHER, proposal["id"])["status"] == "published"
    try:
        admin.publish(CONTRIBUTOR, proposal["id"])
    except PermissionError:
        pass
    else:
        raise AssertionError("contributor reached admin publish action")


def test_workspace_sync_audit_read_model_is_owner_only_and_limits_operational_fields():
    store = Store(":memory:")
    roles = DataRoles(store); roles.bootstrap_owner(OWNER)
    roles.grant(OWNER, REVIEWER, ROLE_REVIEWER, reason="review only")
    WorkspaceAutoSync(store)
    store.conn.execute(
        """INSERT INTO contributor_workspace_sync_audit
           (sync_key,workspace_key,workspace_match_key,workspace_revision,status,code,shared_match_key,detail)
           VALUES(?,?,?,?,?,?,?,?)""",
        ("internal-sync-key", "/private/contributor/workspace.sqlite", "public-listing-10", 2,
         "held_for_review", "not_new", None, "private operational detail"),
    )
    store.conn.commit()

    audit = WorkspaceSyncAuditReadModel(store, roles)
    assert audit.for_owner(OWNER) == [{
        "listing_key": "public-listing-10", "status": "held_for_review",
        "reason": "not_new", "provenance": "workspace revision 2",
    }]
    try:
        audit.for_owner(REVIEWER)
    except PermissionError as exc:
        assert "owner" in str(exc)
    else:
        raise AssertionError("non-owner read workspace sync audit")


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"{len(tests)} passed — admin workflow is role-gated and audit reads are limited")
