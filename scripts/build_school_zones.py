#!/usr/bin/env python3
"""Freeze Staten Island's elementary school zones into kash/data/si_school_zones.json.

    python scripts/build_school_zones.py                    # fetch from NYC Open Data
    python scripts/build_school_zones.py --input zones.geojson

Source: "School Zones 2024-2025 (Elementary School)", NYC Open Data dataset cmjf-yawu
(the 2017-2018 edition this repo would otherwise have used is now login-gated). 770 zones
citywide, 47 on Staten Island.

Why freeze rather than call an API per row: the same reason kash/data/si_nta.json exists —
a nightly job should not depend on a city endpoint being up, and school zones change about
once a year. Rerun this script when the DOE publishes a new school year.

The zones keep only what the lookup needs, coordinates rounded to 5 decimals (~1 m) and
rings simplified with Douglas-Peucker at roughly 40 m, which is what took the NTA asset from
13,492 vertices to 928 without moving any boundary enough to matter for an address.
"""
from __future__ import annotations

import argparse
import json
import math
import os

DATASET = "https://data.cityofnewyork.us/api/geospatial/cmjf-yawu?method=export&format=GeoJSON"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "kash", "data", "si_school_zones.json")
TOLERANCE_M = 40.0
BOROUGH = "R"          # Staten Island, as it appears in a DBN like 31R019


def _metres_per_degree(lat: float) -> tuple[float, float]:
    return 111_320.0, 111_320.0 * math.cos(math.radians(lat))


def _perp_distance_m(p, a, b) -> float:
    """Distance from point p to segment a-b, in metres (equirectangular is ample here)."""
    lat_m, lon_m = _metres_per_degree(p[1])
    px, py = p[0] * lon_m, p[1] * lat_m
    ax, ay = a[0] * lon_m, a[1] * lat_m
    bx, by = b[0] * lon_m, b[1] * lat_m
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def simplify(ring: list, tolerance: float = TOLERANCE_M) -> list:
    """Douglas-Peucker, iterative so a long coastline ring cannot blow the stack."""
    if len(ring) < 3:
        return ring
    keep = [False] * len(ring)
    keep[0] = keep[-1] = True
    stack = [(0, len(ring) - 1)]
    while stack:
        start, end = stack.pop()
        worst, worst_i = 0.0, None
        for i in range(start + 1, end):
            d = _perp_distance_m(ring[i], ring[start], ring[end])
            if d > worst:
                worst, worst_i = d, i
        if worst_i is not None and worst > tolerance:
            keep[worst_i] = True
            stack.append((start, worst_i))
            stack.append((worst_i, end))
    return [p for p, k in zip(ring, keep) if k]


def school_label(dbn: str, label: str | None) -> str:
    """31R019 -> 'PS 19'. The zone dataset carries no school name, and the companion
    School Point Locations table is not row-queryable, so the number is what we can state
    truthfully. DBN stays on the record as the authoritative identifier.

    Shared zones name several schools ('31R016,31R074,31R010', label '16, 74, 10') and are
    rendered 'PS 16/74/10', matching how the buyer already writes them ('PS 8/PS 32')."""
    raw = label if label else ",".join(part[3:] for part in dbn.split(","))
    numbers = [p.strip().lstrip("0") or "0" for p in str(raw).split(",") if p.strip()]
    return "PS " + "/".join(numbers) if numbers else "PS ?"


def rings_of(geometry: dict) -> list:
    """Outer rings only. Zone holes are vanishingly rare and a hole would at worst make the
    lookup fall back to 'unknown', never to a wrong school."""
    kind, coords = geometry.get("type"), geometry.get("coordinates") or []
    if kind == "Polygon":
        return [coords[0]] if coords else []
    if kind == "MultiPolygon":
        return [poly[0] for poly in coords if poly]
    return []


def build(features: list) -> list:
    zones, before, after = [], 0, 0
    for f in features:
        props = f.get("properties") or {}
        dbn = str(props.get("dbn") or "")
        if len(dbn) < 4 or dbn[2] != BOROUGH:
            continue
        polys = []
        for ring in rings_of(f.get("geometry") or {}):
            before += len(ring)
            simple = [[round(x, 5), round(y, 5)] for x, y in simplify(ring)]
            after += len(simple)
            if len(simple) >= 3:
                polys.append(simple)
        if not polys:
            continue
        zones.append({
            "dbn": dbn,
            "school": school_label(dbn, props.get("label")),
            "district": int(float(props.get("schooldist") or 31)),
            "polys": polys,
        })
    zones.sort(key=lambda z: z["dbn"])
    print(f"{len(zones)} Staten Island zones, {before} vertices -> {after}")
    return zones


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", help="a local GeoJSON export (default: fetch the dataset)")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    if args.input:
        data = json.load(open(args.input, encoding="utf-8"))
    else:
        import requests
        print(f"fetching {DATASET}")
        resp = requests.get(DATASET, headers={"User-Agent": UA}, timeout=180)
        resp.raise_for_status()
        data = resp.json()

    zones = build(data.get("features") or [])
    if not zones:
        raise SystemExit("no Staten Island zones found — has the dataset schema changed?")
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(zones, fh, separators=(",", ":"))
    print(f"wrote {args.out} ({os.path.getsize(args.out) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
