#!/usr/bin/env python3
"""Proposal queue visibility is role-scoped before any dashboard is exposed."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.contributions import ContributionService  # noqa: E402
from kash.data_roles import DataRoles, ROLE_CONTRIBUTOR, ROLE_REVIEWER  # noqa: E402
from kash.review_queue import ReviewQueue  # noqa: E402
from kash.store import Store  # noqa: E402

OWNER, CONTRIBUTOR_A, CONTRIBUTOR_B, REVIEWER, READER = 1, 2, 3, 4, 5


def test_contributor_sees_only_own_proposals_and_reviewer_sees_pending_queue():
    store = Store(":memory:")
    roles = DataRoles(store); roles.bootstrap_owner(OWNER)
    for user, role in [(CONTRIBUTOR_A, ROLE_CONTRIBUTOR), (CONTRIBUTOR_B, ROLE_CONTRIBUTOR), (REVIEWER, ROLE_REVIEWER)]:
        roles.grant(OWNER, user, role, reason="approved")
    contributions = ContributionService(store, roles)
    a = contributions.submit_add(CONTRIBUTOR_A, {"street_address": "10 A Road", "zip": "10301"},
                                 source_url="https://example.com/a", observed_at="2026-08-06")
    contributions.submit_add(CONTRIBUTOR_B, {"street_address": "20 B Road", "zip": "10302"},
                             source_url="https://example.com/b", observed_at="2026-08-06")
    queue = ReviewQueue(store, roles)
    assert [p["id"] for p in queue.for_actor(CONTRIBUTOR_A)] == [a["id"]]
    assert len(queue.for_actor(REVIEWER, status="pending")) == 2
    try:
        queue.for_actor(READER)
    except PermissionError:
        pass
    else:
        raise AssertionError("ordinary reader saw contribution queue")


if __name__ == "__main__":
    test_contributor_sees_only_own_proposals_and_reviewer_sees_pending_queue()
    print("1 passed — proposal queue visibility is least-privilege")
