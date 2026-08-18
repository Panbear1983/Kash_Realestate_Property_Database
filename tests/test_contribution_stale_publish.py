#!/usr/bin/env python3
"""Publishing rejects a correction when its reviewed target changed after submission."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.contributions import ContributionService  # noqa: E402
from kash.data_roles import DataRoles, ROLE_CONTRIBUTOR, ROLE_PUBLISHER, ROLE_REVIEWER  # noqa: E402
from kash.store import Store  # noqa: E402

OWNER, CONTRIBUTOR, REVIEWER, PUBLISHER = 1, 2, 3, 4


def test_publish_rejects_stale_correction_without_overwriting_newer_listing_data():
    store = Store(":memory:")
    store.upsert({"street_address": "10 Example Road", "zip": "10301", "list_price": 700000,
                  "status": "active", "source": "fixture"}, "fixture")
    key = store.conn.execute("SELECT match_key FROM listings").fetchone()[0]
    roles = DataRoles(store); roles.bootstrap_owner(OWNER)
    for user, role in [(CONTRIBUTOR, ROLE_CONTRIBUTOR), (REVIEWER, ROLE_REVIEWER), (PUBLISHER, ROLE_PUBLISHER)]:
        roles.grant(OWNER, user, role, reason="approved")
    service = ContributionService(store, roles)
    proposal = service.submit_correction(CONTRIBUTOR, key, {"list_price": 695000},
                                         source_url="https://example.com/10", observed_at="2026-08-05")
    service.approve(REVIEWER, proposal["id"], reason="source checked")
    store.update_fields(key, {"list_price": 690000})
    try:
        service.publish(PUBLISHER, proposal["id"])
    except ValueError as exc:
        assert "changed since submission" in str(exc)
    else:
        raise AssertionError("stale correction overwrote newer data")
    assert store.get(key)["list_price"] == 690000


if __name__ == "__main__":
    test_publish_rejects_stale_correction_without_overwriting_newer_listing_data()
    print("1 passed — stale correction cannot overwrite newer listing data")
