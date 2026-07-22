"""Rotating scrape coverage.

A single daily search only ever catches the newest listings, so older inventory never
gets pulled. Instead, walk the price range one band per run — over ~1-2 weeks the sweep
covers the whole range (the full Staten Island inventory in the buyer's band), then repeats
to catch changes. The sweep index lives in the run-state file.
"""
from __future__ import annotations


def bands(prefs: dict) -> list[tuple]:
    """Split the preference price band into slices of `sweep.band_step` (default 80k)."""
    p = prefs.get("price") or {}
    lo, hi = p.get("min"), p.get("max")
    if not (lo and hi):
        return [(None, None)]
    step = int((prefs.get("sweep") or {}).get("band_step", 80000))
    out, a = [], int(lo)
    while a < hi:
        b = min(a + step, int(hi))
        out.append((a, b))
        a = b
    return out or [(int(lo), int(hi))]


def next_slice(prefs: dict, state: dict) -> tuple:
    """Return this run's (min, max) price slice and advance the sweep index in `state`."""
    bs = bands(prefs)
    idx = int(state.get("sweep_idx", 0)) % len(bs)
    state["sweep_idx"] = (idx + 1) % len(bs)
    return bs[idx]
