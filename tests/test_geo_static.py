#!/usr/bin/env python3
"""Offline neighbourhood assignment from frozen NYC NTA polygons.

No network. The pool.db checks are read-only and skip cleanly when it isn't present.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import geo_static as gs  # noqa: E402

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pool.db")

# Reference points, chosen to be unambiguous rather than borderline.
GREAT_KILLS = (40.5518, -74.1516)
WESTERLEIGH = (40.6166, -74.1382)
MIDLAND_BEACH = (40.5717, -74.0907)
MANHATTAN = (40.7580, -73.9855)      # far outside Staten Island
ATLANTIC = (40.4000, -73.9000)       # open water


def _curated():
    if not os.path.exists(DB):
        return []
    # Read-only URI: this is the LIVE production pool, and a bare connect() opens it rw
    # (holding a write-capable handle on the production WAL during every suite run).
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return [dict(r) for r in c.execute(
        "SELECT street_address,neighborhood,latitude,longitude FROM listings "
        "WHERE property_id IS NOT NULL AND neighborhood IS NOT NULL AND latitude IS NOT NULL")]


# --- polygons load and place points ---------------------------------------------------------

def test_known_points_land_in_the_right_nta():
    assert gs.locate_nta(*GREAT_KILLS) == "Great Kills-Eltingville"
    assert gs.locate_nta(*WESTERLEIGH) == "Westerleigh-Castleton Corners"
    assert gs.locate_nta(*MIDLAND_BEACH) == "New Dorp-Midland Beach"


def test_points_outside_staten_island_return_none():
    assert gs.locate_nta(*MANHATTAN) is None
    assert gs.locate_nta(*ATLANTIC) is None
    assert gs.neighborhood(*MANHATTAN) is None


def test_missing_coordinates_return_none():
    assert gs.locate_nta(None, None) is None
    assert gs.neighborhood(None, -74.15) is None


def test_unambiguous_ntas_resolve_directly():
    assert gs.neighborhood(*WESTERLEIGH) == "Westerleigh"


def test_split_nta_picks_the_nearest_candidate():
    """Great Kills-Eltingville holds two names; the point is deep in Great Kills."""
    assert gs.neighborhood(*GREAT_KILLS) == "Great Kills"


# --- vocabulary and data integrity ----------------------------------------------------------

def test_every_candidate_in_a_split_nta_has_a_centroid():
    """Without a centroid, a split NTA silently degrades to its first candidate."""
    for nta, cands in gs.NTA_CANDIDATES.items():
        if len(cands) > 1:
            missing = [c for c in cands if c not in gs.CENTROIDS]
            assert not missing, f"{nta} candidates missing centroids: {missing}"


def test_centroids_sit_inside_their_own_nta():
    """A centroid that falls outside its own polygon means the table is wrong."""
    bad = []
    for nta, cands in gs.NTA_CANDIDATES.items():
        for name in cands:
            if name not in gs.CENTROIDS:
                continue
            lat, lon = gs.CENTROIDS[name]
            if gs.locate_nta(lat, lon) != nta:
                bad.append((name, nta, gs.locate_nta(lat, lon)))
    # South Beach and Dongan Hills legitimately appear under two NTAs; allow those.
    hard = [b for b in bad if b[0] not in ("South Beach", "Dongan Hills")]
    assert not hard, f"centroids outside their NTA: {hard}"


def test_polygon_file_is_present_and_covers_staten_island():
    areas = gs._areas()
    assert len(areas) >= 20, f"expected the full SI NTA set, got {len(areas)}"
    assert all(a.get("polys") for a in areas)


# --- accuracy against the curated set -------------------------------------------------------

def test_simplified_polygon_boundary_gap_uses_nearest_nta_only_within_50m():
    # This curated-coordinate point is 3.5m outside a simplified shared border, not outside SI.
    assert gs.locate_nta(40.569345, -74.143675) == "Oakwood-Richmondtown"
    assert gs.locate_nta(40.6892, -74.0445) is None  # Statue of Liberty: not Staten Island


def test_all_pool_rows_place_inside_an_nta():
    rows = _curated()
    if not rows:
        return
    unplaced = [r for r in rows if gs.locate_nta(r["latitude"], r["longitude"]) is None]
    assert not unplaced, f"{len(unplaced)} curated rows fell outside every polygon"


def test_agreement_with_hand_labels_does_not_regress():
    """77% at the time of writing. Not 100%, and that is expected: NTAs are administrative
    while the curated labels are colloquial real-estate names, and on the South Shore the two
    genuinely disagree. This guards against a change making it materially worse."""
    rows = _curated()
    if not rows:
        return
    hits = sum(1 for r in rows
               if gs.neighborhood(r["latitude"], r["longitude"]) == r["neighborhood"])
    assert hits / len(rows) >= 0.70, f"agreement fell to {hits}/{len(rows)}"


def test_produced_names_stay_within_the_declared_vocabulary():
    rows = _curated()
    if not rows:
        return
    allowed = {n for names in gs.NTA_CANDIDATES.values() for n in names}
    produced = {gs.neighborhood(r["latitude"], r["longitude"]) for r in rows} - {None}
    assert produced <= allowed, f"unexpected names: {produced - allowed}"


def test_disagreements_reports_without_mutating():
    rows = _curated()
    if not rows:
        return
    before = [dict(r) for r in rows]
    out = gs.disagreements(rows)
    assert rows == before, "disagreements() must not modify the rows it inspects"
    for d in out:
        assert d["labelled"] != d["implies"]


def test_disagreements_ignores_rows_without_a_label_or_coords():
    assert gs.disagreements([{"neighborhood": None, "latitude": 40.55, "longitude": -74.15}]) == []
    assert gs.disagreements([{"neighborhood": "Great Kills", "latitude": None,
                              "longitude": None}]) == []


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
