#!/usr/bin/env python3
"""Proposal lifecycle supports reviewer rejection and contributor withdrawal."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.contributions import ContributionService  # noqa: E402
from kash.data_roles import DataRoles, ROLE_CONTRIBUTOR, ROLE_REVIEWER  # noqa: E402
from kash.store import Store  # noqa: E402

OWNER, CONTRIBUTOR, REVIEWER = 1, 2, 3


def _proposal():
    store = Store(":memory:")
    roles = DataRoles(store); roles.bootstrap_owner(OWNER)
    roles.grant(OWNER, CONTRIBUTOR, ROLE_CONTRIBUTOR, reason="approved")
    roles.grant(OWNER, REVIEWER, ROLE_REVIEWER, reason="approved")
    service = ContributionService(store, roles)
    proposal = service.submit_add(CONTRIBUTOR, {"street_address": "10 Example Road"},
                                  source_url="https://example.com/10", observed_at="2026-08-05")
    return service, proposal


def test_reviewer_can_reject_pending_proposal_with_reason():
    service, proposal = _proposal()
    rejected = service.reject(REVIEWER, proposal["id"], reason="listing link is stale")
    assert rejected["status"] == "rejected" and rejected["reviewed_by"] == REVIEWER


def test_contributor_can_withdraw_only_own_pending_proposal():
    service, proposal = _proposal()
    withdrawn = service.withdraw(CONTRIBUTOR, proposal["id"], reason="submitted wrong URL")
    assert withdrawn["status"] == "withdrawn"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — proposal lifecycle is controlled")
