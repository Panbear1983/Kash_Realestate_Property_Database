#!/usr/bin/env python3
"""Self-contained end-to-end test of the Phase 2 pipeline.

Runnable directly (`python tests/test_pipeline.py`) or under pytest. Uses a temp DB so
it never touches pool.db or private property data. Verifies: seed load, new listings,
price-drop merge
with user-note preservation, status-change tracking, and calc-field recomputation.
"""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import pipeline, preferences          # noqa: E402
from kash.adapters.mock import MockAdapter        # noqa: E402
from kash.dedup import match_key                  # noqa: E402
from kash.schema import FIELD_ORDER                # noqa: E402
from kash.store import Store                       # noqa: E402


def write_seed(path):
    rows = [
        {
            "property_id": "TEST-001", "rank": 1, "status": "active", "tier": "S",
            "street_address": "45 Fairlawn Loop", "zip": "10308",
            "neighborhood": "Great Kills", "property_type": "sf_attached",
            "list_price": 709900, "sqft": 1974, "analysis": "Curated analysis",
            "my_notes": "Private note must survive provider updates",
        },
        {
            "property_id": "TEST-002", "rank": 2, "status": "active", "tier": "A",
            "street_address": "354 Doane Ave", "zip": "10308",
            "neighborhood": "Great Kills", "property_type": "2fam_detached",
            "list_price": 898989, "sqft": 2400, "analysis": "Second curated example",
        },
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELD_ORDER)
        writer.writeheader()
        writer.writerows(rows)


def test_pipeline():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "test.db")
    seed_csv = os.path.join(tmp, "seed.csv")
    write_seed(seed_csv)
    prefs = preferences.derive_from_csv(seed_csv)

    store = Store(db, finance_cfg=prefs.get("finance"))
    seeded = store.seed_from_csv(seed_csv)
    assert seeded == 2, f"expected 2 seed rows, got {seeded}"
    assert store.count() == 2

    fairlawn = match_key({"street_address": "45 Fairlawn Loop", "zip": "10308"})
    before = store.get(fairlawn)
    assert before is not None, "seed row for 45 Fairlawn Loop missing"
    assert before["list_price"] == 709900
    saved_notes = before["my_notes"]
    saved_analysis = before["analysis"]
    assert saved_notes, "expected a user note on the seed row"

    summary = pipeline.run(MockAdapter(), store, prefs)

    # two brand-new addresses inserted; two existing rows merged
    assert summary["inserted"] == 2, summary
    assert summary["updated"] == 2, summary
    assert store.count() == 4

    # price drop merged, original preserved, user notes untouched
    after = store.get(fairlawn)
    assert after["list_price"] == 699000
    assert after["original_list_price"] == 709900
    assert after["price_history"] and after["price_history"][-1]["price"] == 699000
    assert after["my_notes"] == saved_notes, "user note was overwritten!"
    assert after["analysis"] == saved_analysis, "analysis was overwritten!"
    assert after["price_per_sqft"] == round(699000 / 1974)   # recomputed
    assert after["monthly_piti"] and after["monthly_piti"] > 0
    assert after["source"] == "mock"

    # status change tracked
    doane = store.get(match_key({"street_address": "354 Doane Ave", "zip": "10308"}))
    assert doane["status"] == "pending"

    # changelog captured the transitions
    events = {e["event"] for e in summary["events"]}
    assert {"price_drop", "status_change", "new_listing"} <= events, events

    store.close()
    print("PASS — 4 rows, 2 inserted, 2 merged; notes preserved; calc + changelog OK")


if __name__ == "__main__":
    test_pipeline()
