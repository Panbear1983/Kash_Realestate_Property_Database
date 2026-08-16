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


def validate_source_names(names, registry) -> list[str]:
    """Reject configured/manual sources that lack a registered adapter."""
    names = list(names)
    unsupported = sorted(set(names) - set(registry))
    if unsupported:
        raise ValueError(f"Unsupported source(s): {', '.join(unsupported)}")
    return names


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
        # HARD allow-list: the pipeline admits only these ZIPs (kash/pipeline.py::_in_scope).
        # Derived from the seed, so it is exactly where you've been shopping — widen it by hand
        # to discover new areas, or set it to [] to fall back to zip_prefixes alone.
        "zips": sorted(zips),
        # Coarser boundary used only when `zips` is empty: SI ZIPs are 103xx (drops the NJ
        # bleed that the Zillow map bounds pull in).
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
        # NL 'ask' mode backends. `backend: auto` walks `ladder` and skips any rung that is
        # unavailable (CLI missing/logged out, no API key) or that errors, so one dead provider
        # can't take the assistant down. Pin `backend` to a single name to disable the ladder
        # and use exactly that rung. codex = ChatGPT subscription via the codex CLI; claude_cli
        # = Claude subscription via the claude CLI; both are OAuth, no API key and no per-token
        # billing. anthropic = the Claude API, metered, off the ladder unless you add a key.
        # available_ttl caps how often a rung is probed (codex's probe shells out to
        # `codex login status`). Timeouts in seconds.
        "llm": {
            "backend": "auto",
            # Per-job ladders: chat has a user waiting so it is ordered for latency, while rank
            # is a nightly judgment call where depth matters and slow rungs are acceptable.
            "ladders": {
                "chat": ["codex", "claude_cli"],
                "extract": ["claude_cli", "codex"],
                "rank": ["codex", "claude_cli", "agy_cli"],
            },
            "ladder": ["codex", "claude_cli"],
            "timeout": 120,
            "available_ttl": 60,
            # Per-rung daily request budgets. Each rung is a separate subscription, so a user
            # who exhausts one rolls onto the next provider's quota rather than draining one.
            # Requests, not tokens: codex and agy_cli report no token counts, and codex answers
            # most chat questions, so tokens would leave the dominant path unmetered.
            "budgets": {
                "default": {"requests_per_day": 40},
                "system": {"requests_per_day": 200},
                "per_actor": {},
            },
            "backends": {
                "codex": {"bin": "codex", "model": None, "timeout": 120},
                "claude_cli": {"bin": "claude", "model": "haiku", "timeout": 120},
                "agy_cli": {"bin": "agy", "model": "gemini-3.5-flash-low",
                            "timeout": 180, "retries": 2},
                "anthropic": {"model": "claude-haiku-4-5", "max_tokens": 1024, "timeout": 60},
            },
        },
        # Per-source scrape cadence + caps. Zillow discovers new listings (per-result
        # billing, so it queries only recent days-on-market); the RentCast city census
        # re-sights the whole market every other day (per-request billing — free depth).
        "sources": {
            "zillow": {"every_days": 1, "results_limit": 40, "max_days_on_market": 7},
            "rentcast": {"every_days": 2, "results_limit": 500},
        },
        # Rotating coverage: sweep the price range one band per run (see kash/sweep.py).
        "sweep": {"band_step": 80000},
        "rank": {"max_per_run": 15},       # auto-ranks up to N new listings / run
        "enrich": {"max_per_run": 50},     # geocode + flood + neighborhood / run
        "detail": {"max_per_run": 15},     # Zillow detail scrape up to N new rows / run
        "describe": {"max_per_run": 15},   # LLM description signals / run (idempotent per row)
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
