#!/usr/bin/env python3
"""Dashboard contribution-review guards and routing, without launching Textual."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import (  # noqa: E402
    ContributionReviewController,
    contributor_workspace_sync_audit_access,
    contribution_review_access,
)
from kash.data_roles import ROLE_OWNER  # noqa: E402


class FakeRoles:
    def __init__(self, roles):
        self._roles = roles

    def role_for(self, actor_id):
        return self._roles.get(actor_id)


class FakeQueue:
    def __init__(self, roles):
        self.roles = roles


class FakeAdmin:
    def __init__(self, roles):
        self.queue = FakeQueue(roles)
        self.calls = []

    def list(self, actor_id):
        self.calls.append(("list", actor_id))
        return [{"id": 9, "status": "pending"}]

    def approve(self, actor_id, proposal_id, *, reason):
        self.calls.append(("approve", actor_id, proposal_id, reason))
        return {"status": "approved"}

    def reject(self, actor_id, proposal_id, *, reason):
        self.calls.append(("reject", actor_id, proposal_id, reason))
        return {"status": "rejected"}

    def publish(self, actor_id, proposal_id):
        self.calls.append(("publish", actor_id, proposal_id))
        return {"status": "published"}

    def rollback(self, actor_id, proposal_id, *, reason):
        self.calls.append(("rollback", actor_id, proposal_id, reason))
        return {"outcome": "reinstated"}

    def list_workspace_sync_audit(self, actor_id):
        self.calls.append(("sync_audit", actor_id))
        return [{
            "listing_key": "public-listing-10", "status": "held_for_review",
            "reason": "missing_observed_at", "provenance": "workspace revision 1",
        }]


def test_review_is_disabled_without_configured_owner_data_role():
    admin = FakeAdmin(FakeRoles({7: "reviewer"}))
    assert contribution_review_access(admin, None)[0] is False
    enabled, message = contribution_review_access(admin, 7)
    assert enabled is False
    assert "no owner data role" in message


def test_owner_controller_lists_and_routes_only_through_admin():
    admin = FakeAdmin(FakeRoles({7: ROLE_OWNER}))
    assert contribution_review_access(admin, 7)[0] is True
    controller = ContributionReviewController(admin, 7)
    assert controller.list_staged() == [{"id": 9, "status": "pending"}]
    assert controller.act("approve", 9, reason="source verified")["status"] == "approved"
    assert controller.act("reject", 9, reason="duplicate")["status"] == "rejected"
    assert controller.act("publish", 9)["status"] == "published"
    assert controller.act("rollback", 9, reason="incorrect retirement")["outcome"] == "reinstated"
    assert admin.calls == [
        ("list", 7),
        ("approve", 7, 9, "source verified"),
        ("reject", 7, 9, "duplicate"),
        ("publish", 7, 9),
        ("rollback", 7, 9, "incorrect retirement"),
    ]


def test_reasons_are_required_before_admin_action():
    admin = FakeAdmin(FakeRoles({7: ROLE_OWNER}))
    controller = ContributionReviewController(admin, 7)
    for action in ("approve", "reject", "rollback"):
        try:
            controller.act(action, 9)
        except ValueError as exc:
            assert "reason is required" in str(exc)
        else:
            raise AssertionError(f"{action} reached the admin service without a reason")
    assert admin.calls == []


def test_workspace_sync_audit_is_disabled_without_owner_and_never_reads_admin():
    admin = FakeAdmin(FakeRoles({7: "reviewer"}))
    enabled, message = contributor_workspace_sync_audit_access(admin, 7)
    assert enabled is False
    assert "owner data role" in message
    controller = ContributionReviewController(admin, 7)
    assert controller.workspace_sync_audit() == {"enabled": False, "rows": []}
    assert admin.calls == []


def test_owner_controller_routes_limited_workspace_sync_audit_through_admin():
    admin = FakeAdmin(FakeRoles({7: ROLE_OWNER}))
    controller = ContributionReviewController(admin, 7)
    assert controller.workspace_sync_audit() == {
        "enabled": True,
        "rows": [{
            "listing_key": "public-listing-10", "status": "held_for_review",
            "reason": "missing_observed_at", "provenance": "workspace revision 1",
        }],
    }
    assert admin.calls == [("sync_audit", 7)]


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"{len(tests)} passed — dashboard review is owner-gated and uses the admin workflow")
