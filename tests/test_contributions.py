#!/usr/bin/env python3
"""Staged listing proposals are typed, sourced, and role-gated."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.contributions import ContributionService  # noqa: E402
from kash.data_roles import DataRoles, ROLE_CONTRIBUTOR  # noqa: E402
from kash.store import Store  # noqa: E402

OWNER, CONTRIBUTOR = 1, 2


def _service():
    store = Store(":memory:")
    roles = DataRoles(store)
    roles.bootstrap_owner(OWNER)
    roles.grant(OWNER, CONTRIBUTOR, ROLE_CONTRIBUTOR, reason="approved contributor")
    return ContributionService(store, roles)


def test_contributor_can_submit_a_sourced_add_to_staging_only():
    service = _service()
    proposal = service.submit_add(CONTRIBUTOR, {
        "street_address": "10 Example Road", "zip": "10301", "list_price": 700000,
        "status": "active", "listing_url": "https://example.com/listing/10",
    }, source_url="https://example.com/listing/10", observed_at="2026-08-04")
    assert proposal["status"] == "pending" and proposal["kind"] == "add"
    assert service.store.count() == 0


def test_private_listing_fields_are_rejected_from_contributor_proposals():
    service = _service()
    try:
        service.submit_add(CONTRIBUTOR, {"street_address": "10 Example Road", "my_notes": "secret"},
                           source_url="https://example.com/listing/10", observed_at="2026-08-04")
    except ValueError as exc:
        assert "my_notes" in str(exc)
    else:
        raise AssertionError("private field entered the proposal")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — contribution proposals are staged and validated")
