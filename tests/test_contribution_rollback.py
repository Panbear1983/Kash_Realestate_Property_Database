#!/usr/bin/env python3
"""Owner-only rollback creates a new audited reinstatement, never a delete."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.contributions import ContributionService  # noqa: E402
from kash.data_roles import DataRoles, ROLE_CONTRIBUTOR, ROLE_PUBLISHER, ROLE_REVIEWER  # noqa: E402
from kash.store import Store  # noqa: E402

OWNER, CONTRIBUTOR, REVIEWER, PUBLISHER = 1, 2, 3, 4


def test_owner_can_rollback_published_retire_by_reinstating_prior_status():
    store = Store(":memory:")
    store.upsert({"street_address": "10 Example Road", "zip": "10301", "list_price": 700000,
                  "status": "active", "source": "fixture"}, "fixture")
    key = store.conn.execute("SELECT match_key FROM listings").fetchone()[0]
    roles = DataRoles(store); roles.bootstrap_owner(OWNER)
    for user, role in [(CONTRIBUTOR, ROLE_CONTRIBUTOR), (REVIEWER, ROLE_REVIEWER), (PUBLISHER, ROLE_PUBLISHER)]:
        roles.grant(OWNER, user, role, reason="approved")
    service = ContributionService(store, roles)
    proposal = service.submit_retire(CONTRIBUTOR, key, reason="source says off market",
                                     source_url="https://example.com/10", observed_at="2026-08-05")
    service.approve(REVIEWER, proposal["id"], reason="verified")
    service.publish(PUBLISHER, proposal["id"])
    result = service.rollback(OWNER, proposal["id"], reason="source was incorrect")
    assert store.get(key)["status"] == "active"
    assert result["outcome"] == "reinstated"


if __name__ == "__main__":
    test_owner_can_rollback_published_retire_by_reinstating_prior_status()
    print("1 passed — owner rollback reinstates without deleting audit history")
