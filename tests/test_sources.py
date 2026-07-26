#!/usr/bin/env python3
"""Source-registry contract tests; no provider credentials or network required."""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import preferences  # noqa: E402
from kash.adapters import REGISTRY  # noqa: E402


def _seed_csv(path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["zip", "property_type", "neighborhood", "list_price"])
        writer.writeheader()
        writer.writerow({"zip": "10308", "property_type": "sf_detached",
                         "neighborhood": "Great Kills", "list_price": "700000"})


def test_derived_sources_are_implemented_adapters():
    with tempfile.TemporaryDirectory() as tmp:
        seed = os.path.join(tmp, "seed.csv")
        _seed_csv(seed)
        prefs = preferences.derive_from_csv(seed)
    assert set(prefs["sources"]) <= set(REGISTRY), prefs["sources"]


def test_manual_source_selection_rejects_unsupported_provider_names():
    try:
        preferences.validate_source_names(["rentcast", "redfin"], REGISTRY)
    except ValueError as exc:
        assert "redfin" in str(exc)
    else:
        raise AssertionError("unsupported source must be rejected before scheduling")


if __name__ == "__main__":
    test_derived_sources_are_implemented_adapters()
    test_manual_source_selection_rejects_unsupported_provider_names()
    print("PASS — derived source configuration matches implemented adapters")
