"""Offline neighbourhood assignment from coordinates — no API, no key, deterministic.

Replaces the ZIP heuristic in kash/enrich/runner.py, which copied the *dominant* neighbourhood
per ZIP from rows that already had one. That was circular (a ZIP with no labelled row got
nothing, which is why every unfilled row sat in 10305/10301/10303) and inaccurate (10314 alone
holds five distinct neighbourhoods, so every unlabelled 10314 row was stamped "Westerleigh").

How this works instead:

1. Point-in-polygon against NYC's official 2020 Neighborhood Tabulation Areas, frozen into
   kash/data/si_nta.json (23 Staten Island areas, Douglas-Peucker simplified to ~40m —
   13,492 vertices down to 928, 20 KB). Authoritative and offline.
2. NTAs are coarser than the buyer's vocabulary — "Great Kills-Eltingville" is one area, and
   so is "New Springville-Willowbrook-Bulls Head-Travis". So each NTA maps to a small candidate
   list, and the nearest candidate centroid wins. Disambiguating 2-4 genuinely adjacent
   neighbourhoods is far better conditioned than a global nearest-centroid search.

A caveat worth stating plainly, because it limits what this can promise: NTA boundaries are
administrative, while the labels in the curated set are colloquial real-estate designations, and
on the South Shore the two genuinely disagree. Annadale and Huguenot labels fall inside the
Arden Heights-Rossville polygon and vice versa. Neither is wrong — they are different naming
systems. Consequently this module **only fills blanks and never overwrites an existing
neighbourhood**; a human label always wins. `disagreements()` reports the mismatches for review
rather than silently "correcting" them.
"""
from __future__ import annotations

import json
import math
import os
from typing import Optional

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "si_nta.json")

# NTA -> the buyer's vocabulary. Single-entry lists are unambiguous; multi-entry lists are the
# NTAs that the buyer's naming splits, resolved by nearest centroid below. North Shore areas
# keep their own names: the pool has no curated examples there, and a plausible local name beats
# a null.
NTA_CANDIDATES: dict[str, list[str]] = {
    "Great Kills-Eltingville": ["Great Kills", "Eltingville"],
    "New Springville-Willowbrook-Bulls Head-Travis": [
        "Bulls Head", "New Springville", "Heartland Village"],
    "Annadale-Huguenot-Prince's Bay-Woodrow": ["Annadale", "Huguenot"],
    "Arden Heights-Rossville": ["Arden Heights", "Rossville"],
    "Oakwood-Richmondtown": ["Oakwood", "Richmond Town"],
    "New Dorp-Midland Beach": ["New Dorp", "Midland Beach"],
    "Todt Hill-Emerson Hill-Lighthouse Hill-Manor Heights": ["High Rock Pk", "Dongan Hills"],
    "Westerleigh-Castleton Corners": ["Westerleigh"],
    "Grasmere-Arrochar-South Beach-Dongan Hills": ["Dongan Hills", "South Beach"],
    "Tottenville-Charleston": ["Tottenville"],
    "Port Richmond": ["Port Richmond"],
    "St. George-New Brighton": ["St George"],
    "Tompkinsville-Stapleton-Clifton-Fox Hills": ["Tompkinsville"],
    "Rosebank-Shore Acres-Park Hill": ["Rosebank"],
    "Mariner's Harbor-Arlington-Graniteville": ["Mariners Harbor"],
    "West New Brighton-Silver Lake-Grymes Hill": ["West New Brighton"],
    "Fort Wadsworth": ["Fort Wadsworth"],
}

# Centroids for the split cases, in degrees. Derived from the curated rows that fall inside an
# NTA listing their own name (which excludes known label/geography outliers), and hand-supplied
# for names the curated set has no clean example of.
CENTROIDS: dict[str, tuple[float, float]] = {
    "Great Kills":       (40.55175, -74.15163),
    "Eltingville":       (40.54450, -74.16400),   # no clean curated example
    "Bulls Head":        (40.61580, -74.16501),
    "New Springville":   (40.60172, -74.16172),
    "Heartland Village": (40.59308, -74.16042),
    # The South Shore four are set from real neighbourhood centres, each verified to fall
    # inside its own NTA, rather than derived from curated rows. Those rows carry the
    # colloquial-vs-administrative disagreement documented above: labels reading "Annadale"
    # and "Huguenot" sit inside the Arden Heights-Rossville polygon, so a derived centroid
    # would land in the wrong area and propagate the error into every future assignment.
    "Annadale":          (40.54050, -74.17830),
    "Huguenot":          (40.53400, -74.19150),
    "Arden Heights":     (40.55790, -74.19820),
    "Rossville":         (40.54460, -74.21390),
    "Oakwood":           (40.56087, -74.12196),
    "Richmond Town":     (40.56980, -74.13285),
    "New Dorp":          (40.56488, -74.10492),
    "Midland Beach":     (40.57168, -74.09070),
    "High Rock Pk":      (40.57750, -74.12717),
    "Dongan Hills":      (40.59843, -74.09478),
    "South Beach":       (40.58300, -74.07600),   # no curated example
}

_AREAS: Optional[list] = None


def _areas() -> list:
    global _AREAS
    if _AREAS is None:
        with open(_DATA, encoding="utf-8") as fh:
            _AREAS = json.load(fh)
    return _AREAS


def _in_ring(lat: float, lon: float, ring: list) -> bool:
    """Ray-casting point-in-polygon. Ring vertices are [lon, lat] per GeoJSON."""
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            x_int = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < x_int:
                inside = not inside
    return inside


def _point_segment_metres(lat, lon, a, b) -> float:
    """Distance to one GeoJSON [lon, lat] segment at Staten Island scale."""
    scale = math.cos(math.radians(lat))
    px, py = lon * scale, lat
    ax, ay = a[0] * scale, a[1]
    bx, by = b[0] * scale, b[1]
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    t = 0.0 if not denom else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy)) * 111_000


def locate_nta(lat: float, lon: float) -> Optional[str]:
    """Official containing NTA, with a narrow fallback for simplified-boundary gaps."""
    if lat is None or lon is None:
        return None
    nearest = (float("inf"), None)
    for area in _areas():
        for poly in area["polys"]:
            # poly[0] is the outer ring; any further rings are holes.
            if _in_ring(lat, lon, poly[0]) and not any(_in_ring(lat, lon, h) for h in poly[1:]):
                return area["name"]
            ring = poly[0]
            for i, point in enumerate(ring):
                distance = _point_segment_metres(lat, lon, point, ring[(i + 1) % len(ring)])
                if distance < nearest[0]:
                    nearest = (distance, area["name"])
    # The frozen polygons are simplified to ~40 m.  Assigning only points within 50 m closes
    # slivers along shared boundaries without turning genuinely out-of-area coordinates into SI.
    return nearest[1] if nearest[0] <= 50 else None


def _metres(lat1, lon1, lat2, lon2) -> float:
    """Equirectangular approximation — ample at neighbourhood scale."""
    return math.hypot((lat1 - lat2) * 111_000,
                      (lon1 - lon2) * 111_000 * math.cos(math.radians(lat1)))


def neighborhood(lat: float, lon: float) -> Optional[str]:
    """Best neighbourhood name for a point, or None if it can't be placed."""
    nta = locate_nta(lat, lon)
    if nta is None:
        return None
    candidates = NTA_CANDIDATES.get(nta)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    known = [(c, CENTROIDS[c]) for c in candidates if c in CENTROIDS]
    if not known:
        return candidates[0]
    return min(known, key=lambda kv: _metres(lat, lon, kv[1][0], kv[1][1]))[0]


def disagreements(rows) -> list[dict]:
    """Rows whose existing label differs from what the geography implies.

    Reported, never auto-applied — see the module docstring on why both can be defensible.
    """
    out = []
    for r in rows:
        have = r.get("neighborhood")
        lat, lon = r.get("latitude"), r.get("longitude")
        if not have or lat is None or lon is None:
            continue
        nta = locate_nta(lat, lon)
        if nta and have not in (NTA_CANDIDATES.get(nta) or []):
            out.append({"street_address": r.get("street_address"), "labelled": have,
                        "nta": nta, "implies": neighborhood(lat, lon)})
    return out
