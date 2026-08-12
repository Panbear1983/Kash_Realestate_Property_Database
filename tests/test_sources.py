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


# --- the Zillow search URL carries the buyer's filters -------------------------------------
# resultsLimit caps the run BEFORE our scope filter sees anything, so every filter missing
# from the query wastes result slots on listings the pipeline discards (measured: 35-78%).

def _decoded_filter_state(**kwargs):
    import json
    import urllib.parse
    from kash.adapters.zillow_scraper import SI_BOUNDS, _search_url
    url = _search_url("Staten Island, NY", SI_BOUNDS, **kwargs)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["searchQueryState"][0]
    return json.loads(q)["filterState"]


def test_search_url_carries_beds_baths_and_price():
    fs = _decoded_filter_state(price={"min": 560915, "max": 640915},
                               beds_min=3, baths_min=2)
    assert fs["beds"] == {"min": 3}
    assert fs["baths"] == {"min": 2}, "whole-number baths must not serialize as 2.0"
    assert fs["price"] == {"min": 560915, "max": 640915}
    assert fs["sortSelection"] == {"value": "days"}


def test_search_url_excludes_the_rejected_property_types():
    fs = _decoded_filter_state(excluded_types=["condo", "apartment", "lot"])
    for key in ("isCondo", "isApartment", "isApartmentOrCondo", "isLotLand"):
        assert fs[key] == {"value": False}, key


def test_unknown_excluded_type_is_ignored_not_fatal():
    fs = _decoded_filter_state(excluded_types=["houseboat"])
    assert "houseboat" not in str(fs)


def test_bare_search_url_is_unchanged():
    fs = _decoded_filter_state()
    assert set(fs) == {"sortSelection"}, "no prefs -> no filters, as before"


if __name__ == "__main__":
    test_derived_sources_are_implemented_adapters()
    test_manual_source_selection_rejects_unsupported_provider_names()
    test_search_url_carries_beds_baths_and_price()
    test_search_url_excludes_the_rejected_property_types()
    test_unknown_excluded_type_is_ignored_not_fatal()
    test_bare_search_url_is_unchanged()
    print("PASS — source config + Zillow query filters")
