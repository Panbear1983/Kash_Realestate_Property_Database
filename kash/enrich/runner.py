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
    """Fill a missing neighbourhood, preferring coordinates over the ZIP heuristic.

    `kash.geo_static` resolves a point against frozen NYC NTA polygons — offline, exact, and
    able to name areas the pool has never seen. The old ZIP-dominant fallback is kept only for
    rows without coordinates: it cannot fill a ZIP with no labelled example (which is why every
    unfilled row sat in 10305/10301/10303) and it mislabels ZIPs holding several neighbourhoods.

    Existing labels are never overwritten — a human label always wins. Use
    `geo_static.disagreements()` to review conflicts rather than silently rewriting them.
    """
    from .. import geo_static

    rows = store.all()
    zmap = _zip_neighborhood_map(rows)
    n = 0
    for r in rows:
        if r.get("neighborhood"):
            continue
        name = geo_static.neighborhood(r.get("latitude"), r.get("longitude"))
        if not name:
            name = zmap.get(r.get("zip"))
        if name:
            key = match_key(r)
            if key:
                store.update_fields(key, {"neighborhood": name})
                n += 1
    return n


def enrich(store, limit: Optional[int] = None, sleep: float = 0.3, ledger=None) -> dict:
    """Geocode rows lacking coordinates, then flood-zone rows lacking a zone.

    Every attempt is recorded to the ledger when one is supplied, so a failing source is
    visible afterwards instead of collapsing into an error count — and rows in backoff are
    skipped rather than re-hammering a source that is known to be down.
    """
    from ..ledger import Ledger

    if ledger is None:
        ledger = Ledger(store)

    rows = store.all()
    stats = {"processed": 0, "geocoded": 0, "flood_zoned": 0, "errors": 0,
             "flood_unavailable": 0, "deferred": 0}
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
            if ledger.is_due(key, "latitude"):
                try:
                    g = geo.geocode(r.get("street_address"), r.get("zip"))
                    if g:
                        updates.update(g)
                        lat, lon = g["latitude"], g["longitude"]
                        stats["geocoded"] += 1
                        ledger.record_success(key, "latitude")
                    else:
                        ledger.record_failure(key, "latitude", "no address match")
                except Exception as e:  # noqa: BLE001
                    stats["errors"] += 1
                    ledger.record_failure(key, "latitude", f"{type(e).__name__}: {e}")
                time.sleep(sleep)
            else:
                stats["deferred"] += 1

        if needs_flood:
            if lat is None or lon is None:
                # Not a provider failure — this row simply isn't ready yet.
                ledger.record_skip(key, "flood_zone", "no coordinates")
            elif ledger.is_due(key, "flood_zone"):
                try:
                    zone, status = fl.flood_zone_status(lat, lon)
                    if status in (fl.RESOLVED, fl.NOT_MAPPED):
                        updates["flood_zone"] = zone
                        stats["flood_zoned"] += 1
                        ledger.record_success(key, "flood_zone")
                    else:
                        # Never write a zone we could not verify: an unavailable lookup must
                        # not read as safe and let the row through the alert gate.
                        stats["flood_unavailable"] += 1
                        ledger.record_failure(key, "flood_zone", "all providers unavailable")
                except Exception as e:  # noqa: BLE001
                    stats["errors"] += 1
                    ledger.record_failure(key, "flood_zone", f"{type(e).__name__}: {e}")
                time.sleep(sleep)
            else:
                stats["deferred"] += 1

        if updates:
            store.update_fields(key, updates)
        stats["processed"] += 1

    stats["neighborhoods"] = fill_neighborhoods(store)
    return stats
