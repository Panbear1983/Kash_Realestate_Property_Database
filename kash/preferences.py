"""Preference profile — the search criteria that drives every adapter.

Auto-derived from the current pool (the docx-seeded CSV) so the scraper looks for the
same kind of property you've been tracking: SI ZIPs, price band, types, neighborhoods.
Written to preferences.yaml; edit it by hand to tune the search.
"""
from __future__ import annotations

import csv
import os
from typing import Optional

import yaml


def derive_from_csv(csv_path: str) -> dict:
    zips, types, hoods, prices = set(), set(), set(), []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("zip"):
                zips.add(row["zip"].strip())
            if row.get("property_type"):
                types.add(row["property_type"].strip())
            if row.get("neighborhood"):
                hoods.add(row["neighborhood"].strip())
            try:
                prices.append(int(row["list_price"]))
            except (KeyError, ValueError, TypeError):
                pass
    lo = int(min(prices) * 0.85) if prices else None
    hi = int(max(prices) * 1.15) if prices else None
    return {
        "market": "Staten Island, NY",
        "zips": sorted(zips),
        # keep results on-island: SI ZIPs are 103xx (drops NJ bleed from map bounds)
        "zip_prefixes": sorted({z[:3] for z in zips}) or ["103"],
        "price": {"min": lo, "max": hi},
        "property_types": sorted(types),
        "neighborhoods": sorted(hoods),
        "beds_min": 3,
        "fetch_limit": 100,
        "finance": {
            "rate": 0.063,
            "down_payment_pct": 0.20,
            "term_years": 30,
        },
        # NL 'ask' mode backend. codex = ChatGPT via OAuth (no API key). model: null =
        # subscription default. timeout in seconds.
        "llm": {"backend": "codex", "model": None, "timeout": 120},
        # Per-source scrape cadence + caps. Staggered to spread Apify credit.
        "sources": {
            "zillow": {"every_days": 1, "results_limit": 40},
            "realtor": {"every_days": 2, "results_limit": 40},
            "redfin": {"every_days": 3, "results_limit": 40},
            "rentcast": {"every_days": 7},
        },
        # Rotating coverage: sweep the price range one band per run (see kash/sweep.py).
        "sweep": {"band_step": 80000},
        "rank": {"max_per_run": 15},       # Codex auto-ranks up to N new listings / run
        "enrich": {"max_per_run": 50},     # geocode + flood + neighborhood / run
        "detail": {"max_per_run": 15},     # Zillow detail scrape up to N new rows / run
    }


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def save(prefs: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(prefs, f, sort_keys=False, default_flow_style=False)


def ensure(path: str, csv_path: str) -> dict:
    """Load preferences.yaml, deriving + writing it from the CSV if absent."""
    if not os.path.exists(path):
        prefs = derive_from_csv(csv_path)
        save(prefs, path)
        return prefs
    return load(path)
