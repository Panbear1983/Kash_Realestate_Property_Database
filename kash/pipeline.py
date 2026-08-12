"""The fetch pipeline: fetch -> validate -> scope-filter -> dedup/merge -> summary.

Adapters return raw source records; this module owns validation (against the schema),
scope filtering (against preferences), and the merge into the pool store. Dedup and the
user-field protection live in Store.upsert().
"""
from __future__ import annotations

import re
from typing import Optional

from pydantic import ValidationError

from .adapters.base import SourceAdapter
from . import eligibility
from .dedup import match_key
from .schema import Listing
from .store import Store


def _first_number(value):
    """First numeric value in provider text — sources emit ranges like '3-4' for beds."""
    if value is None:
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(match.group()) if match else None


def _in_scope(rec: dict, prefs: dict) -> bool:
    z = rec.get("zip")
    # The explicit ZIP allow-list is the buyer's actual target area; zip_prefixes is the
    # coarser fallback that only drops off-island bleed (the Zillow map bounds overlap NJ).
    zips = prefs.get("zips")
    if zips and z:
        if str(z) not in {str(v) for v in zips}:
            return False
    else:
        prefixes = prefs.get("zip_prefixes")
        if prefixes and z and not any(str(z).startswith(p) for p in prefixes):
            return False

    band = prefs.get("price") or {}
    price = rec.get("list_price")
    if price is not None:
        if band.get("min") and price < band["min"]:
            return False
        if band.get("max") and price > band["max"]:
            return False

    # A range like '3-4' is admitted on its low end, so a 3+ search keeps '3-4' listings.
    beds_min = prefs.get("beds_min")
    if beds_min:
        beds = _first_number(rec.get("beds"))
        if beds is not None and beds < float(beds_min):
            return False

    # NOTE: `neighborhoods` is deliberately NOT filtered here. orchestrator.update() runs this
    # pipeline before enrich's fill_neighborhoods, so the field is still null at ingest and
    # filtering on it would reject nearly every incoming row.
    return eligibility.classify(rec, prefs).admit


def run(adapter: SourceAdapter, store: Store, prefs: dict) -> dict:
    before = store.conn.execute("SELECT COALESCE(MAX(id),0) FROM changelog").fetchone()[0]
    raw = adapter.fetch(prefs)

    tally = {"inserted": 0, "updated": 0, "unchanged": 0,
             "out_of_scope": 0, "rejected": 0}
    seen_keys = []
    for item in raw:
        try:
            rec = Listing(**item).model_dump()
        except ValidationError:
            tally["rejected"] += 1
            continue
        if not _in_scope(rec, prefs):
            tally["out_of_scope"] += 1
            continue
        outcome = store.upsert(rec, source=adapter.name)
        tally[outcome] = tally.get(outcome, 0) + 1
        if outcome != "rejected":
            seen_keys.append(match_key(rec))

    events = store.conn.execute(
        "SELECT event, detail, match_key FROM changelog WHERE id>? ORDER BY id",
        (before,),
    ).fetchall()
    return {
        "source": adapter.name,
        "fetched": len(raw),
        **tally,
        "pool_size": store.count(),
        "events": [dict(e) for e in events],
        # What this run saw, and what its absence proves (kash/lifecycle.py).
        "seen_keys": [k for k in seen_keys if k],
        "coverage": adapter.coverage(len(raw)),
    }
