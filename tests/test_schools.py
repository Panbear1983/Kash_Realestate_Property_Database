#!/usr/bin/env python3
"""Zoned elementary school from frozen DOE polygons. Offline — the asset ships in the repo.

The claim "your kids would go to PS 44" has to be right or absent, so most of these tests are
about refusing to answer: outside the island, no coordinates, junk input. The frozen asset was
checked against the unsimplified source dataset on every geocoded row in the live pool — 36 of
36 identical — so simplification is not moving addresses across boundaries.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import schools  # noqa: E402
from kash.dedup import match_key  # noqa: E402
from kash.store import Store  # noqa: E402

# Points taken from the source dataset, not from memory.
GREAT_KILLS = (40.5541, -74.1522, "PS 32")
TOTTENVILLE = (40.5083, -74.2515, "PS 1")
ST_GEORGE = (40.6437, -74.0736, "PS 16/74/10")     # a shared zone naming three schools


def row(addr="1 Test Ave", **over):
    r = {"street_address": addr, "zip": "10308", "list_price": 700000, "beds": "3",
         "baths": "2", "property_type": "sf_detached",
         "latitude": GREAT_KILLS[0], "longitude": GREAT_KILLS[1]}
    r.update(over)
    return r


def store_with(*rows):
    s = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    for r in rows:
        s.upsert(r, source="zillow")
    return s


# --- the asset ------------------------------------------------------------------------------

def test_the_frozen_asset_covers_staten_island():
    z = schools.zones()
    assert len(z) == 47, "47 elementary zones on Staten Island in the 2024-2025 map"
    assert all(zone["district"] == 31 for zone in z), "Staten Island is district 31"
    assert all(zone["school"].startswith("PS ") for zone in z)
    assert all(zone["polys"] and len(zone["polys"][0]) >= 3 for zone in z)


def test_every_dbn_is_a_staten_island_dbn():
    for zone in schools.zones():
        for part in zone["dbn"].split(","):
            assert part[2] == "R", zone["dbn"]


# --- lookups --------------------------------------------------------------------------------

def test_known_points_land_in_their_zone():
    for lat, lon, expected in (GREAT_KILLS, TOTTENVILLE, ST_GEORGE):
        found = schools.zoned_school(lat, lon)
        assert found and found["school"] == expected, (lat, lon, found)
        assert found["school_year"] == schools.SCHOOL_YEAR


def test_off_island_is_none_not_a_nearest_guess():
    assert schools.zoned_school(40.7580, -73.9855) is None      # midtown Manhattan
    assert schools.zoned_school(40.45, -74.05) is None          # the ocean
    assert schools.zoned_school(0, 0) is None


def test_missing_or_junk_coordinates_are_none():
    for lat, lon in ((None, None), (40.55, None), ("", ""), ("abc", "def")):
        assert schools.zoned_school(lat, lon) is None


def test_school_for_row_reads_the_row_coordinates():
    assert schools.school_for_row(row())["school"] == "PS 32"
    assert schools.school_for_row({"latitude": None, "longitude": None}) is None


# --- filling --------------------------------------------------------------------------------

def test_a_blank_school_is_filled_from_the_zone():
    s = store_with(row())
    stats = schools.fill_school_zones(s)
    assert stats["filled"] == 1
    assert s.get(match_key(row()))["school_name"] == "PS 32"


def test_a_hand_entered_name_is_never_overwritten():
    """'PS 29 Bardwell' is richer than anything this lookup can produce."""
    s = store_with(row(school_name="PS 29 Bardwell"))
    stats = schools.fill_school_zones(s)
    assert stats["filled"] == 0 and stats["candidates"] == 0
    assert s.get(match_key(row()))["school_name"] == "PS 29 Bardwell"


def test_rows_without_coordinates_are_counted_not_guessed():
    s = store_with(row(latitude=None, longitude=None))
    stats = schools.fill_school_zones(s)
    assert stats["filled"] == 0 and stats["no_coordinates"] == 1
    assert s.get(match_key(row()))["school_name"] is None


def test_an_off_island_row_is_left_empty():
    s = store_with(row(latitude=40.7580, longitude=-73.9855))
    stats = schools.fill_school_zones(s)
    assert stats["outside_all_zones"] == 1 and stats["filled"] == 0


def test_filling_is_idempotent_and_resumable():
    rows = [row(f"{i} Test Ave") for i in range(5)]
    s = store_with(*rows)
    first = schools.fill_school_zones(s, limit=2)
    assert first["filled"] == 2 and first["candidates"] == 5
    second = schools.fill_school_zones(s, limit=10)
    assert second["filled"] == 3, "the rest are picked up on the next run"
    assert schools.fill_school_zones(s)["filled"] == 0


# --- provenance -----------------------------------------------------------------------------

def test_disagreements_are_reported_not_reconciled():
    s = store_with(row(school_name="PS 99"))
    conflicts = schools.disagreements(s.all())
    assert len(conflicts) == 1
    assert conflicts[0]["recorded"] == "PS 99" and conflicts[0]["zoned"] == "PS 32"
    assert s.get(match_key(row()))["school_name"] == "PS 99", "reporting must not rewrite"


def test_a_shared_zone_counts_as_agreement_when_either_school_matches():
    s = store_with(row(latitude=ST_GEORGE[0], longitude=ST_GEORGE[1], school_name="PS 74"))
    assert schools.disagreements(s.all()) == []


def test_stats_line_reads_plainly():
    text = schools.format_stats({"filled": 3, "candidates": 10, "no_coordinates": 1,
                                 "outside_all_zones": 2})
    assert "3 zoned" in text and "10 without one" in text


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
