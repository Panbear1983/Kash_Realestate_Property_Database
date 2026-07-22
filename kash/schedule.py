"""Per-source cadence — decide which sources are due today and remember last runs.

The routine fires daily (launchd), but each source runs only every N days per its
`every_days` config, so Zillow can run daily while RentCast runs weekly (staying inside
each provider's free tier). State is a tiny JSON file (.last_run.json).
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime


def load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save(path: str, state: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def due(name: str, cfg: dict, state: dict) -> bool:
    every = int((cfg or {}).get("every_days", 1))
    last = state.get(name)
    if not last:
        return True
    try:
        last_d = datetime.strptime(last, "%Y-%m-%d").date()
    except ValueError:
        return True
    return (date.today() - last_d).days >= every


def which_due(names: list[str], prefs: dict, state: dict) -> list[str]:
    cfgs = prefs.get("sources", {})
    return [n for n in names if due(n, cfgs.get(n, {}), state)]


def mark(name: str, state: dict) -> None:
    state[name] = date.today().isoformat()
