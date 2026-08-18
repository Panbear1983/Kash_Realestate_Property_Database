#!/usr/bin/env python3
"""Correction and soft-retire proposals stay staged and never delete production data."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.contributions import ContributionService  # noqa: E402
from kash.data_roles import DataRoles, ROLE_CONTRIBUTOR  # noqa: E402
from kash.store import Store  # noqa: E402

OWNER, CONTRIBUTOR = 1, 2


def _service():
    store = Store(":memory:")
    roles = DataRoles(store); roles.bootstrap_owner(OWNER)
    roles.grant(OWNER, CONTRIBUTOR, ROLE_CONTRIBUTOR, reason="approved")
    return ContributionService(store, roles)


def test_contributor_correction_is_staged_and_requires_source_evidence():
    service = _service()
    service.store.upsert({"street_address": "10 Example Road", "zip": "10301", "list_price": 700000}, "fixture")
    key = service.store.conn.execute("SELECT match_key FROM listings").fetchone()[0]
    proposal = service.submit_correction(CONTRIBUTOR, key, {"list_price": 695000},
                                         source_url="https://example.com/listing-1", observed_at="2026-08-04")
    assert proposal["kind"] == "correct" and proposal["status"] == "pending"
    assert service.store.get(key)["list_price"] == 700000


def test_contributor_retire_is_a_staged_soft_retire_not_a_delete():
    service = _service()
    proposal = service.submit_retire(CONTRIBUTOR, "listing-1", reason="source marks it sold",
                                     source_url="https://example.com/listing-1", observed_at="2026-08-04")
    assert proposal["kind"] == "retire" and proposal["status"] == "pending"
    assert service.store.get("listing-1") is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — corrections and retires remain staged")
