"""Per-source cadence — decide which sources are due today and remember last runs.

The routine fires daily (launchd), but each source runs only every N days per its
`every_days` config, so Zillow can run daily while RentCast runs weekly (staying inside
each provider's free tier). State is a tiny JSON file (.last_run.json).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime

log = logging.getLogger("kash.schedule")


def load(path: str) -> dict:
    """Read the run state. A corrupt file is renamed aside rather than silently discarded.

    Both 'file absent' and 'file present but unparseable' used to return {} identically, so a
    truncated state file looked like a first run: every source would be treated as due and the
    sweep index would silently reset to band 0.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        try:
            os.replace(path, path + ".corrupt")
            log.error("run state at %s was unreadable; moved to %s.corrupt and starting fresh",
                      path, path)
        except OSError:
            log.error("run state at %s is unreadable and could not be moved aside", path)
        return {}
    except OSError as e:
        log.error("could not read run state %s: %s", path, e)
        return {}


def save(path: str, state: dict) -> None:
    """Write atomically: a crash mid-write previously left a truncated file, which load()
    then treated as a first run."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


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
    """Record a SUCCESSFUL run. Only call this when the source actually returned data.

    Marking unconditionally is how a dead credential goes quiet: RentCast returned 403, the
    run marked it as having run today, and its 7-day cadence then meant the next attempt was a
    week away — a broken key backing off into silence rather than being retried.
    """
    state[name] = date.today().isoformat()
    fails = state.setdefault("consecutive_failures", {})
    fails.pop(name, None)


def mark_failure(name: str, state: dict) -> int:
    """Record a failed run and return how many times this source has failed in a row."""
    fails = state.setdefault("consecutive_failures", {})
    fails[name] = int(fails.get(name, 0)) + 1
    return fails[name]


def failure_count(name: str, state: dict) -> int:
    return int((state.get("consecutive_failures") or {}).get(name, 0))
