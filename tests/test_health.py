#!/usr/bin/env python3
"""Run-health verdicts, cadence marking, and sweep advancement.

The failure this encodes: on the 26 July run RentCast returned 403 and the geocoder failed on
28 of 28 rows, and the owner got a normal digest with launchd recording exit 0. Nothing could
say "that went badly". No network.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import health, schedule, sweep  # noqa: E402

PREFS = {"price": {"min": 560915, "max": 800000}, "sweep": {"band_step": 80000}}


def result(**over):
    base = {"summaries": [{"source": "zillow", "fetched": 40, "inserted": 2}],
            "enrich": {"processed": 10, "errors": 0}, "detail": {}, "describe": {}, "rank": {},
            "backup": "/tmp/pool-x.db", "completeness": {"alert_blocked": 0}}
    base.update(over)
    return base


# --- verdicts -------------------------------------------------------------------------------

def test_a_clean_run_is_ok():
    assert health.assess(result())["verdict"] == health.OK


def test_a_failing_source_is_degraded_not_ok():
    r = result(summaries=[{"source": "zillow", "fetched": 40},
                          {"source": "rentcast", "error": "403 Forbidden"}])
    h = health.assess(r)
    assert h["verdict"] == health.DEGRADED
    assert any("rentcast" in p for p in h["problems"])


def test_every_source_failing_is_a_failure():
    r = result(summaries=[{"source": "zillow", "error": "boom"},
                          {"source": "rentcast", "error": "403"}])
    assert health.assess(r)["verdict"] == health.FAILED


def test_an_aborted_run_is_a_failure():
    assert health.assess({"aborted": "backup failed: disk full"})["verdict"] == health.FAILED


def test_mass_enrichment_failure_is_degraded():
    """28 of 28 geocoder failures must not read as a healthy run."""
    h = health.assess(result(enrich={"processed": 28, "errors": 28}))
    assert h["verdict"] == health.DEGRADED
    assert any("28 of 28" in p for p in h["problems"])


def test_a_few_enrichment_errors_are_tolerated():
    assert health.assess(result(enrich={"processed": 28, "errors": 2}))["verdict"] == health.OK


def test_zero_fetched_across_all_sources_is_degraded():
    assert health.assess(result(summaries=[{"source": "zillow", "fetched": 0}]))["verdict"] \
        == health.DEGRADED


def test_a_failed_backup_shows_up_in_the_verdict():
    h = health.assess(result(backup="backup failed: row counts differ"))
    assert h["verdict"] == health.DEGRADED
    assert any("backup failed" in p for p in h["problems"])


def test_blocked_alerts_are_reported():
    h = health.assess(result(completeness={"alert_blocked": 7}))
    assert any("7 listings blocked" in p for p in h["problems"])


def test_a_stage_error_is_reported():
    assert health.assess(result(rank={"error": "no backend"}))["verdict"] == health.DEGRADED


def test_flood_unavailable_is_reported():
    h = health.assess(result(enrich={"processed": 10, "errors": 0, "flood_unavailable": 5}))
    assert any("flood lookup unavailable" in p for p in h["problems"])


# --- the alert message ----------------------------------------------------------------------

def test_failed_alert_says_nothing_was_collected():
    msg = health.format_alert(health.assess({"aborted": "backup failed"}))
    assert "FAILED" in msg and "no listings were collected" in msg


def test_degraded_alert_lists_the_problems():
    r = result(summaries=[{"source": "zillow", "fetched": 1},
                          {"source": "rentcast", "error": "403 Forbidden"}])
    msg = health.format_alert(health.assess(r))
    assert "403" in msg and "update.log" in msg


# --- cadence marking ------------------------------------------------------------------------

def test_a_failed_source_is_not_marked_as_run():
    state = {}
    schedule.mark_failure("rentcast", state)
    assert "rentcast" not in state
    assert schedule.failure_count("rentcast", state) == 1


def test_consecutive_failures_accumulate_then_reset_on_success():
    state = {}
    for _ in range(3):
        schedule.mark_failure("rentcast", state)
    assert schedule.failure_count("rentcast", state) == 3
    schedule.mark("rentcast", state)
    assert schedule.failure_count("rentcast", state) == 0
    assert state["rentcast"]


def test_an_unmarked_source_stays_due():
    state = {}
    schedule.mark_failure("rentcast", state)
    assert schedule.due("rentcast", {"every_days": 7}, state) is True


# --- state file safety ----------------------------------------------------------------------

def test_a_corrupt_state_file_is_moved_aside_not_silently_ignored():
    p = os.path.join(tempfile.mkdtemp(), "state.json")
    open(p, "w").write("{not json")
    assert schedule.load(p) == {}
    assert os.path.exists(p + ".corrupt"), "the unreadable file should be preserved for review"


def test_a_missing_state_file_is_simply_empty():
    assert schedule.load(os.path.join(tempfile.mkdtemp(), "nope.json")) == {}


def test_save_is_atomic_and_round_trips():
    p = os.path.join(tempfile.mkdtemp(), "state.json")
    schedule.save(p, {"zillow": "2026-07-27", "sweep_idx": 2})
    assert json.load(open(p))["sweep_idx"] == 2
    assert not os.path.exists(p + ".tmp")


# --- sweep ----------------------------------------------------------------------------------

def test_next_slice_does_not_advance():
    state = {"sweep_idx": 1}
    a = sweep.next_slice(PREFS, state)
    b = sweep.next_slice(PREFS, state)
    assert a == b, "reading the band must not consume it"
    assert state["sweep_idx"] == 1


def test_advance_moves_to_the_next_band():
    state = {"sweep_idx": 0}
    first = sweep.next_slice(PREFS, state)
    sweep.advance(PREFS, state)
    assert sweep.next_slice(PREFS, state) != first


def test_advance_wraps_around():
    state = {"sweep_idx": 0}
    n = len(sweep.bands(PREFS))
    for _ in range(n):
        sweep.advance(PREFS, state)
    assert state["sweep_idx"] == 0


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
