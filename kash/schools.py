"""Which elementary school a house is zoned for, answered offline.

For a family buying a multigenerational home, the zoned school is part of the address, not a
nice-to-have — but only 36 of the pool's rows carry one, all hand-entered. This fills the rest
from frozen NYC DOE zone polygons (kash/data/si_school_zones.json, 47 Staten Island zones,
rebuilt by scripts/build_school_zones.py), the same offline approach kash/geo_static.py uses
for neighbourhoods: no per-row API call, no nightly dependency on a city endpoint.

What it deliberately does not claim: the zone dataset carries a school number, not a name, so
this reports "PS 44" and keeps the DBN alongside. A hand-entered name like "PS 29 Bardwell"
is richer, so filling only ever touches empty fields — see fill_school_zones().

Zone boundaries also change, roughly once a school year, and a zoned school is not a promise
of a seat. The field is machine-owned; `school_verified` stays the buyer's to set.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from .geo_static import _in_ring

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "si_school_zones.json")
_ZONES: Optional[list] = None

# The frozen asset is the 2024-2025 school year, so a stale pool row can be dated later.
SCHOOL_YEAR = "2024-2025"


def zones() -> list:
    global _ZONES
    if _ZONES is None:
        with open(_DATA, encoding="utf-8") as fh:
            _ZONES = json.load(fh)
    return _ZONES


def zoned_school(lat, lon) -> Optional[dict]:
    """The zone containing this point, or None if it is outside every Staten Island zone.

    None is the honest answer for a point in the water, on the wrong island, or in one of the
    gaps between simplified boundaries — never a nearest-zone guess, because "your kids would
    go to PS 44" is the kind of claim that has to be right or absent.
    """
    if lat is None or lon is None:
        return None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    for zone in zones():
        if any(_in_ring(lat, lon, ring) for ring in zone["polys"]):
            return {"dbn": zone["dbn"], "school": zone["school"],
                    "district": zone["district"], "school_year": SCHOOL_YEAR}
    return None


def school_for_row(row: dict) -> Optional[dict]:
    return zoned_school(row.get("latitude"), row.get("longitude"))


def fill_school_zones(store, limit: int = 200) -> dict:
    """Fill `school_name` on rows that have coordinates but no school. Never overwrites:
    the 36 rows with hand-entered names keep them, including richer ones like 'PS 29
    Bardwell' that this lookup could only render as 'PS 29'.
    """
    stats = {"filled": 0, "candidates": 0, "outside_all_zones": 0, "no_coordinates": 0}
    keys = [r[0] for r in store.conn.execute("SELECT match_key FROM listings").fetchall()]
    for key in keys:
        row = store.get(key)
        if row is None or (row.get("school_name") or "").strip():
            continue
        stats["candidates"] += 1
        if stats["filled"] >= limit:
            continue
        if row.get("latitude") is None or row.get("longitude") is None:
            stats["no_coordinates"] += 1
            continue
        found = school_for_row(row)
        if not found:
            stats["outside_all_zones"] += 1
            continue
        store.update_fields(key, {"school_name": found["school"]})
        stats["filled"] += 1
    return stats


def disagreements(rows) -> list[dict]:
    """Hand-entered school names that the DOE zone map does not agree with.

    Measured on the live pool: of 36 hand-entered rows, 11 match the zoned school and 25 do
    not. That is not a bug in either — the entries frequently name two schools ("PS 5/PS 6"),
    which is how a listing site reports nearby or choice schools, while zoning assigns one
    address to one zone. Reported rather than reconciled: overwriting the buyer's own notes
    with a polygon lookup would be the wrong way to settle it.
    """
    out = []
    for row in rows:
        hand = (row.get("school_name") or "").strip()
        if not hand:
            continue
        found = school_for_row(row)
        if not found:
            continue
        stated = {p.strip().lstrip("PS").strip()
                  for p in hand.replace("/", " ").replace(",", " ").split() if p.strip()}
        zoned = {n for n in found["school"].replace("PS", "").replace("/", " ").split()}
        if not (stated & zoned):
            out.append({"address": row.get("street_address"), "recorded": hand,
                        "zoned": found["school"], "dbn": found["dbn"]})
    return out


def format_stats(stats: dict) -> str:
    if stats.get("error"):
        return f"schools: {stats['error']}"
    return (f"schools: {stats.get('filled', 0)} zoned "
            f"({stats.get('candidates', 0)} without one, "
            f"{stats.get('no_coordinates', 0)} lack coordinates, "
            f"{stats.get('outside_all_zones', 0)} outside every zone)")
