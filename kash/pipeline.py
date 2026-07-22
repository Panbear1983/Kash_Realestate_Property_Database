"""The fetch pipeline: fetch -> validate -> scope-filter -> dedup/merge -> summary.

Adapters return raw source records; this module owns validation (against the schema),
scope filtering (against preferences), and the merge into the pool store. Dedup and the
user-field protection live in Store.upsert().
"""
from __future__ import annotations

from typing import Optional

from pydantic import ValidationError

from .adapters.base import SourceAdapter
from .schema import Listing
from .store import Store


def _in_scope(rec: dict, prefs: dict) -> bool:
    # keep results within the market's ZIP prefixes (drops off-island bleed)
    prefixes = prefs.get("zip_prefixes")
    z = rec.get("zip")
    if prefixes and z and not any(str(z).startswith(p) for p in prefixes):
        return False
    band = prefs.get("price") or {}
    price = rec.get("list_price")
    if price is not None:
        if band.get("min") and price < band["min"]:
            return False
        if band.get("max") and price > band["max"]:
            return False
    return True


def run(adapter: SourceAdapter, store: Store, prefs: dict) -> dict:
    before = store.conn.execute("SELECT COALESCE(MAX(id),0) FROM changelog").fetchone()[0]
    raw = adapter.fetch(prefs)

    tally = {"inserted": 0, "updated": 0, "unchanged": 0,
             "out_of_scope": 0, "rejected": 0}
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
    }
