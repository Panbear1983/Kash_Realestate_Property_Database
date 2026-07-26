#!/usr/bin/env python3
"""Scope-filter policy: which scraped listings are admitted to the pool.

These cover the gaps found against the curated docx profile — beds_min and the explicit
`zips` allow-list were previously unenforced, so 2-bedroom homes and North Shore ZIPs
were being admitted. No DB, no network.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.pipeline import _first_number, _in_scope  # noqa: E402

PREFS = {
    "zips": ["10306", "10308", "10312"],
    "zip_prefixes": ["103"],
    "price": {"min": 560915, "max": 800000},
    "beds_min": 3,
    "eligibility": {
        "excluded_property_types": ["condo", "apartment", "lot"],
        "store_min_baths": 2,
    },
}


def row(**over):
    base = {"zip": "10308", "list_price": 700000, "beds": "3", "baths": "2",
            "property_type": "sf_detached"}
    base.update(over)
    return base


# --- beds ---------------------------------------------------------------------------------

def test_two_bed_is_rejected():
    assert _in_scope(row(beds="2"), PREFS) is False


def test_three_bed_is_admitted():
    assert _in_scope(row(beds="3"), PREFS) is True


def test_bed_range_is_admitted_on_its_low_end():
    """Sources emit '3-4'; a 3+ search should keep it."""
    assert _in_scope(row(beds="3-4"), PREFS) is True


def test_bed_range_below_minimum_is_rejected():
    assert _in_scope(row(beds="2-3"), PREFS) is False


def test_missing_beds_is_not_rejected():
    """Beds are often absent on first fetch; enrichment fills them later."""
    assert _in_scope(row(beds=None), PREFS) is True


def test_beds_min_unset_admits_anything():
    prefs = {**PREFS}
    prefs.pop("beds_min")
    assert _in_scope(row(beds="1"), prefs) is True


# --- zips ---------------------------------------------------------------------------------

def test_zip_outside_the_allow_list_is_rejected():
    """10301 is a Staten Island ZIP, so the prefix filter alone would have let it through."""
    assert _in_scope(row(zip="10301"), PREFS) is False


def test_zip_inside_the_allow_list_is_admitted():
    assert _in_scope(row(zip="10312"), PREFS) is True


def test_empty_zips_falls_back_to_prefixes():
    prefs = {**PREFS, "zips": []}
    assert _in_scope(row(zip="10301"), prefs) is True      # on-island, prefix matches
    assert _in_scope(row(zip="07008"), prefs) is False     # NJ bleed still dropped


def test_missing_zip_is_not_rejected():
    assert _in_scope(row(zip=None), PREFS) is True


# --- neighbourhood must NOT be filtered ----------------------------------------------------

def test_null_neighborhood_is_still_admitted():
    """fill_neighborhoods runs after this pipeline, so filtering here would reject everything."""
    prefs = {**PREFS, "neighborhoods": ["Great Kills", "Eltingville"]}
    assert _in_scope(row(neighborhood=None), prefs) is True


def test_unlisted_neighborhood_is_still_admitted():
    prefs = {**PREFS, "neighborhoods": ["Great Kills"]}
    assert _in_scope(row(neighborhood="Tottenville"), prefs) is True


# --- pre-existing policy still holds --------------------------------------------------------

def test_price_above_band_is_rejected():
    assert _in_scope(row(list_price=915000), PREFS) is False


def test_price_below_band_is_rejected():
    assert _in_scope(row(list_price=400000), PREFS) is False


def test_excluded_property_type_is_rejected():
    assert _in_scope(row(property_type="condo"), PREFS) is False


def test_baths_below_storage_minimum_is_rejected():
    assert _in_scope(row(baths="1"), PREFS) is False


# --- helper ---------------------------------------------------------------------------------

def test_first_number_parses_ranges_and_junk():
    assert _first_number("3-4") == 3.0
    assert _first_number("2.5") == 2.5
    assert _first_number("3 beds") == 3.0
    assert _first_number(None) is None
    assert _first_number("n/a") is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
