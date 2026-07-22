"""Enrichment runner — geocode rows that lack coordinates, then flood-zone rows that
have coordinates but no flood zone. Idempotent (skips already-filled rows), bounded by
`limit`, and resilient (one failure doesn't stop the batch).
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from typing import Optional

from ..dedup import match_key
from . import flood as fl
from . import geocode as geo


def _zip_neighborhood_map(rows) -> dict:
    m = defaultdict(Counter)
    for r in rows:
        if r.get("neighborhood") and r.get("zip"):
            m[r["zip"]][r["neighborhood"]] += 1
    return {z: c.most_common(1)[0][0] for z, c in m.items()}


def fill_neighborhoods(store) -> int:
    """Fill a missing neighborhood from its ZIP, using the dominant neighborhood per ZIP
    among rows that already have one (the curated seed). Free, no API call."""
    rows = store.all()
    zmap = _zip_neighborhood_map(rows)
    n = 0
    for r in rows:
        if not r.get("neighborhood") and zmap.get(r.get("zip")):
            key = match_key(r)
            if key:
                store.update_fields(key, {"neighborhood": zmap[r["zip"]]})
                n += 1
    return n


def enrich(store, limit: Optional[int] = None, sleep: float = 0.3) -> dict:
    rows = store.all()
    stats = {"processed": 0, "geocoded": 0, "flood_zoned": 0, "errors": 0}
    for r in rows:
        if limit and stats["processed"] >= limit:
            break
        needs_geo = r.get("latitude") is None or r.get("longitude") is None
        needs_flood = not r.get("flood_zone")
        if not (needs_geo or needs_flood):
            continue
        key = match_key(r)
        if not key:
            continue

        updates: dict = {}
        lat, lon = r.get("latitude"), r.get("longitude")
        if needs_geo:
            try:
                g = geo.geocode(r.get("street_address"), r.get("zip"))
                if g:
                    updates.update(g)
                    lat, lon = g["latitude"], g["longitude"]
                    stats["geocoded"] += 1
            except Exception:  # noqa: BLE001
                stats["errors"] += 1
            time.sleep(sleep)

        if not r.get("flood_zone") and lat is not None and lon is not None:
            try:
                z = fl.flood_zone(lat, lon)
                if z:
                    updates["flood_zone"] = z
                    stats["flood_zoned"] += 1
            except Exception:  # noqa: BLE001
                stats["errors"] += 1
            time.sleep(sleep)

        if updates:
            store.update_fields(key, updates)
        stats["processed"] += 1
    stats["neighborhoods"] = fill_neighborhoods(store)
    return stats
