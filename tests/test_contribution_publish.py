#!/usr/bin/env python3
"""Only a publisher can apply an approved staged add to the production store."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.contributions import ContributionService  # noqa: E402
from kash.data_roles import DataRoles, ROLE_CONTRIBUTOR, ROLE_PUBLISHER, ROLE_REVIEWER  # noqa: E402
from kash.store import Store  # noqa: E402

OWNER, CONTRIBUTOR, REVIEWER, PUBLISHER = 1, 2, 3, 4


def test_only_publisher_can_apply_approved_add_with_audit():
    store = Store(":memory:")
    roles = DataRoles(store); roles.bootstrap_owner(OWNER)
    for user, role in [(CONTRIBUTOR, ROLE_CONTRIBUTOR), (REVIEWER, ROLE_REVIEWER), (PUBLISHER, ROLE_PUBLISHER)]:
        roles.grant(OWNER, user, role, reason="approved")
    service = ContributionService(store, roles)
    proposal = service.submit_add(CONTRIBUTOR, {"street_address": "10 Example Road", "zip": "10301", "list_price": 700000},
                                  source_url="https://example.com/10", observed_at="2026-08-05")
    service.approve(REVIEWER, proposal["id"], reason="source checked")
    result = service.publish(PUBLISHER, proposal["id"])
    assert result["status"] == "published" and store.count() == 1
    assert result["audit"]["proposal_id"] == proposal["id"]


if __name__ == "__main__":
    test_only_publisher_can_apply_approved_add_with_audit()
    print("1 passed — controlled publisher applies approved additions")
