#!/usr/bin/env python3
"""Read-only dashboard acquisition-status tests."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.status import acquisition_summary  # noqa: E402
from kash.store import Store  # noqa: E402


def test_acquisition_summary_explains_current_rules_without_mutating_store():
    store = Store(":memory:")
    prefs = {
        "sources": {"rentcast": {"every_days": 7}, "zillow": {"every_days": 1}},
        "eligibility": {"telegram_min_baths": 2.5},
    }
    summary = acquisition_summary(store, prefs)
    assert summary["pool_size"] == 0
    assert summary["sources"] == ("rentcast (every 7d)", "zillow (every 1d)")
    assert "stores qualifying homes" in summary["policy"]
    assert "2.5+ baths" in summary["policy"]
    assert store.count() == 0
    store.close()


if __name__ == "__main__":
    test_acquisition_summary_explains_current_rules_without_mutating_store()
    print("PASS — dashboard status is read-only and explains acquisition rules")
