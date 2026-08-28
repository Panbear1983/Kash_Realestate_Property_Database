#!/usr/bin/env python3
"""The even-pacing governor: spend the whole cycle budget, never early, never past the
ceiling, and degrade to the conservative fallback — not the ceiling — on any bad meter."""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import apify_pacing as ap  # noqa: E402

NOW = datetime(2026, 8, 28, 7, 0, tzinfo=timezone.utc)
CYCLE_END = "2026-09-27T23:59:59.999Z"


def _budget(used, cap=5.0, cycle_end=CYCLE_END):
    return {"used": used, "cap": cap, "pct": used / cap if cap else 0,
            "cycle_start": "2026-08-28T00:00:00.000Z", "cycle_end": cycle_end}


def test_fresh_cycle_splits_the_target_evenly():
    pace = ap.paced_results_limit(_budget(0.0), now=NOW)
    # $5 * 0.95 over 31 nights ≈ $0.153/night ≈ 17 results at $0.009
    assert pace["nights_left"] == 31
    assert pace["limit"] == 17
    assert 0.15 < pace["allowance"] < 0.16


def test_overspending_shrinks_the_next_night():
    ahead = ap.paced_results_limit(_budget(0.0), now=NOW)["limit"]
    behind = ap.paced_results_limit(_budget(2.5), now=NOW)["limit"]
    assert behind < ahead


def test_underspending_or_cheap_cost_rises_to_the_ceiling_never_past():
    pace = ap.paced_results_limit(_budget(0.0), now=NOW, est_cost_per_result=0.001)
    assert pace["limit"] == ap.DEFAULT_CEILING
    pace = ap.paced_results_limit(_budget(0.0), now=NOW, est_cost_per_result=0.001,
                                  ceiling=25)
    assert pace["limit"] == 25


def test_below_the_floor_means_skip_not_a_tiny_run():
    pace = ap.paced_results_limit(_budget(4.7), now=NOW)   # 5*0.95-4.7 = 0.05 left
    assert pace["limit"] == 0
    assert "skipping" in pace["reason"]


def test_exhausted_or_overrun_budget_always_skips():
    for used in (4.75, 5.0, 5.07):
        assert ap.paced_results_limit(_budget(used), now=NOW)["limit"] == 0, used


def test_last_night_gets_the_whole_remaining_margin_and_nights_never_zero():
    last_night = datetime(2026, 9, 27, 7, 0, tzinfo=timezone.utc)
    pace = ap.paced_results_limit(_budget(4.0), now=last_night)
    assert pace["nights_left"] == 1
    # $0.75 margin / $0.009 = 83 affordable, clamped to the ceiling
    assert pace["limit"] == ap.DEFAULT_CEILING
    after_end = datetime(2026, 9, 28, 7, 0, tzinfo=timezone.utc)
    pace = ap.paced_results_limit(_budget(4.0), now=after_end)
    assert pace["nights_left"] == 1                     # clock skew: never a divide-by-zero


def test_any_unusable_meter_degrades_to_the_fallback_never_the_ceiling():
    cases = [None, "junk", {}, _budget(1.0, cap=0),
             _budget(1.0, cycle_end=None), _budget(1.0, cycle_end="not-a-date"),
             {"used": "x", "cap": 5.0, "cycle_end": CYCLE_END}]
    for bad in cases:
        pace = ap.paced_results_limit(bad, now=NOW)
        assert pace["limit"] == ap.DEFAULT_FALLBACK_LIMIT, bad
        assert "fallback" in pace["reason"]
    assert ap.paced_results_limit(_budget(0.0), now=NOW,
                                  est_cost_per_result=0)["limit"] == ap.DEFAULT_FALLBACK_LIMIT


def test_from_config_reads_the_knobs_and_survives_junk():
    cfg = {"results_limit": 40, "pace_floor": 5, "est_cost_per_result": 0.009,
           "pace_spend_target": 0.95, "pace_fallback_limit": 15}
    knobs = ap.from_config(cfg)
    assert knobs == {"ceiling": 40, "floor": 5, "est_cost_per_result": 0.009,
                     "spend_target": 0.95, "fallback": 15}
    junk = ap.from_config({"results_limit": "many", "pace_floor": -3})
    assert junk["ceiling"] == ap.DEFAULT_CEILING
    assert junk["floor"] == ap.DEFAULT_FLOOR
    # and the knobs plug straight into the governor
    pace = ap.paced_results_limit(_budget(0.0), now=NOW, **ap.from_config(cfg))
    assert pace["limit"] == 17


def test_the_real_limits_shape_flows_end_to_end():
    """apify_budget's parsed dict (with cycle fields) is exactly what the governor eats."""
    from kash import apify_budget

    class R:
        def json(self):
            return {"data": {"current": {"monthlyUsageUsd": 1.0},
                             "limits": {"maxMonthlyUsageUsd": 5},
                             "monthlyUsageCycle": {"startAt": "2026-08-28T00:00:00.000Z",
                                                   "endAt": CYCLE_END}}}

    budget = apify_budget.month_to_date(token="t", request_get=lambda *a, **k: R())
    pace = ap.paced_results_limit(budget, now=NOW)
    assert 0 < pace["limit"] <= ap.DEFAULT_CEILING


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — the $5 cycle is spent evenly and the 403 wall is never hit")
