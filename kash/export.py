"""Export the live pool to CSV — all rows, all fields.

The frozen seed lives in `archive/`; this writes the *current* growing dataset from
pool.db, sorted so the hand-ranked set comes first (by rank) and auto-added scraped rows
follow.
"""
from __future__ import annotations

import csv
import json

from .schema import FIELD_ORDER, JSON_FIELDS


def to_csv(store, path: str) -> int:
    rows = store.all()
    rows.sort(key=lambda r: (r.get("rank") is None, r.get("rank") or 0,
                             r.get("tier") or "Z", -(r.get("list_price") or 0)))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELD_ORDER)
        w.writeheader()
        for r in rows:
            out = {}
            for k in FIELD_ORDER:
                v = r.get(k)
                if k in JSON_FIELDS and v is not None:
                    v = json.dumps(v)
                out[k] = v
            w.writerow(out)
    return len(rows)
