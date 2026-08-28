"""Even-pacing governor for the Apify monthly credit.

Turns the observability read (`apify_budget.month_to_date`) into tonight's Zillow
`results_limit`: the cycle's remaining budget is split into equal nightly allowances, the
server-side meter is re-read every night so any estimation error self-corrects the next
night, and the spend target stops short of the hard cap so a run never hits the 403 wall
(the August blowout: $5.07 by day 20, then three nights of dead enrichment).

Pure functions, no I/O — the caller (run_update) fetches the meter and owns logging. The
cost estimate affects smoothness only, never the ceiling: overspend one night and the next
night's allowance shrinks automatically, because the allowance is derived from the live
meter, not from local accounting.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

DEFAULT_CEILING = 40                 # per-night max even when budget would allow more
DEFAULT_FLOOR = 5                    # fewer affordable results than this -> skip the night
DEFAULT_EST_COST_PER_RESULT = 0.009  # measured ~$0.008-0.009 per search result
DEFAULT_SPEND_TARGET = 0.95          # stop 5% short of the hard cap
DEFAULT_FALLBACK_LIMIT = 15          # known-safe static limit when the meter is unreadable


def _parse_when(value) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def paced_results_limit(budget: Optional[dict], *, now: Optional[datetime] = None,
                        ceiling: int = DEFAULT_CEILING, floor: int = DEFAULT_FLOOR,
                        est_cost_per_result: float = DEFAULT_EST_COST_PER_RESULT,
                        spend_target: float = DEFAULT_SPEND_TARGET,
                        fallback: int = DEFAULT_FALLBACK_LIMIT) -> dict:
    """Tonight's zillow results_limit as {"limit", "reason", ...}.

    limit == 0 means skip tonight's Zillow fetch entirely: below the floor, a run's fixed
    actor overhead buys too little, and the unspent allowance rolls into tomorrow's pace
    on its own. Any unusable meter degrades to the conservative static fallback — never
    to the ceiling."""
    if not isinstance(budget, dict):
        return {"limit": int(fallback), "reason": "meter unavailable; static fallback"}
    used, cap = budget.get("used"), budget.get("cap")
    cycle_end = _parse_when(budget.get("cycle_end"))
    if (cycle_end is None or not isinstance(used, (int, float))
            or not isinstance(cap, (int, float)) or cap <= 0
            or not est_cost_per_result or est_cost_per_result <= 0):
        return {"limit": int(fallback), "reason": "meter incomplete; static fallback"}

    now = now or datetime.now(timezone.utc)
    remaining = cap * float(spend_target) - float(used)
    nights_left = max(1, math.ceil((cycle_end - now).total_seconds() / 86400.0))
    allowance = remaining / nights_left
    affordable = int(allowance // float(est_cost_per_result)) if allowance > 0 else 0
    if affordable < int(floor):
        return {"limit": 0, "allowance": allowance, "nights_left": nights_left,
                "reason": (f"${max(allowance, 0.0):.2f} nightly allowance affords "
                           f"{max(affordable, 0)} results (< floor {int(floor)}); "
                           "skipping tonight")}
    limit = min(affordable, int(ceiling))
    return {"limit": limit, "allowance": allowance, "nights_left": nights_left,
            "reason": (f"${allowance:.2f} allowance x {nights_left} nights left "
                       f"at ~${float(est_cost_per_result):.3f}/result")}


def from_config(cfg: dict) -> dict:
    """Pacing knobs from a sources.zillow config block, with safe defaults."""
    def _num(key, default, cast):
        try:
            value = cast(cfg.get(key))
            return value if value > 0 else default
        except (TypeError, ValueError):
            return default
    return {
        "ceiling": _num("results_limit", DEFAULT_CEILING, int),
        "floor": _num("pace_floor", DEFAULT_FLOOR, int),
        "est_cost_per_result": _num("est_cost_per_result",
                                    DEFAULT_EST_COST_PER_RESULT, float),
        "spend_target": _num("pace_spend_target", DEFAULT_SPEND_TARGET, float),
        "fallback": _num("pace_fallback_limit", DEFAULT_FALLBACK_LIMIT, int),
    }
