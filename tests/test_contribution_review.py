#!/usr/bin/env python3
"""Review workflow preserves separation of duties."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.contributions import ContributionService  # noqa: E402
from kash.data_roles import DataRoles, ROLE_CONTRIBUTOR, ROLE_REVIEWER  # noqa: E402
from kash.store import Store  # noqa: E402

OWNER, CONTRIBUTOR, REVIEWER = 1, 2, 3


def _service():
    store = Store(":memory:")
    roles = DataRoles(store); roles.bootstrap_owner(OWNER)
    roles.grant(OWNER, CONTRIBUTOR, ROLE_CONTRIBUTOR, reason="approved")
    roles.grant(OWNER, REVIEWER, ROLE_REVIEWER, reason="approved")
    service = ContributionService(store, roles)
    proposal = service.submit_add(CONTRIBUTOR, {"street_address": "10 Example Road"},
                                  source_url="https://example.com/10", observed_at="2026-08-04")
    return service, proposal


def test_contributor_cannot_approve_their_own_proposal():
    service, proposal = _service()
    try:
        service.approve(CONTRIBUTOR, proposal["id"], reason="looks good")
    except PermissionError:
        pass
    else:
        raise AssertionError("contributor self-approved")


def test_separate_reviewer_approves_with_attributed_audit_record():
    service, proposal = _service()
    approved = service.approve(REVIEWER, proposal["id"], reason="source checked")
    assert approved["status"] == "approved" and approved["reviewed_by"] == REVIEWER


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — proposal review requires separation of duties")
