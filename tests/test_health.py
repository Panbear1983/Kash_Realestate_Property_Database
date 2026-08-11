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


def _days_ago(n):
    from datetime import date, timedelta
    return (date.today() - timedelta(days=n)).isoformat()


def test_a_failed_source_is_due_again_tomorrow_not_next_week():
    """A failure must not push the retry a whole cadence away — but not to today either."""
    state = {}
    schedule.mark_failure("rentcast", state)                             # stamps today
    assert schedule.due("rentcast", {"every_days": 7}, state) is False   # not the same day…
    state["last_failure"]["rentcast"] = _days_ago(1)
    assert schedule.due("rentcast", {"every_days": 7}, state) is True    # …but tomorrow


def test_failures_back_off_1_2_4_days():
    """Daily retries after a blip are what burned RentCast's 50-call monthly quota."""
    for fails, wait in ((1, 1), (2, 2), (3, 4)):
        state = {"consecutive_failures": {"rentcast": fails},
                 "last_failure": {"rentcast": _days_ago(wait - 1)}}
        assert schedule.due("rentcast", {"every_days": 7}, state) is False, \
            f"{fails} failures must wait {wait}d, was due at {wait - 1}d"
        state["last_failure"]["rentcast"] = _days_ago(wait)
        assert schedule.due("rentcast", {"every_days": 7}, state) is True


def test_backoff_is_capped_at_the_source_cadence():
    state = {"consecutive_failures": {"rentcast": 10},        # 2**9 = 512d uncapped
             "last_failure": {"rentcast": _days_ago(7)}}
    assert schedule.due("rentcast", {"every_days": 7}, state) is True


def test_success_clears_the_backoff_stamp():
    state = {}
    for _ in range(3):
        schedule.mark_failure("rentcast", state)
    schedule.mark("rentcast", state)
    assert schedule.failure_count("rentcast", state) == 0
    assert "rentcast" not in state["last_failure"]


def test_legacy_state_with_failures_but_no_attempt_date_stays_due():
    """State files written before the backoff carry counts but no last_failure stamp."""
    state = {"consecutive_failures": {"rentcast": 3}}
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


# --- problem signatures and the alert delta -------------------------------------------------
# The unconditional nightly alert re-sent the same message every run: ten identical RentCast
# messages. alert_delta remembers what was already reported (in the run-state file) and gates
# the phone; the log still gets the full verdict every run.

def degraded(*problems):
    return {"verdict": health.DEGRADED, "problems": list(problems)}


def test_signature_ignores_url_noise_but_keeps_the_status_code():
    a = health.signature("source rentcast: 403 Client Error: Forbidden "
                         "for url: https://api.rentcast.io/v1/listings?zip=10304&limit=40")
    b = health.signature("source rentcast: 403 Client Error: Forbidden "
                         "for url: https://api.rentcast.io/v1/listings?zip=10312&limit=40")
    assert a == b
    assert "403" in a and "10304" not in a


def test_signature_treats_differing_row_counts_as_the_same_problem():
    assert health.signature("enrichment failed on 28 of 28 rows") == \
        health.signature("enrichment failed on 27 of 28 rows")


def test_signature_distinguishes_status_codes():
    assert health.signature("source rentcast: 403") != health.signature("source rentcast: 400")


def test_three_identical_degraded_runs_send_exactly_one_alert():
    state, sent = {}, []
    for day in ("2026-08-01", "2026-08-02", "2026-08-03"):
        m = health.alert_delta(degraded("source rentcast: 403 Forbidden"), state, today=day)
        if m:
            sent.append(m)
    assert len(sent) == 1 and "New:" in sent[0] and "403" in sent[0]


def test_a_second_problem_alerts_without_repeating_the_first():
    state = {}
    health.alert_delta(degraded("source rentcast: 403"), state, today="2026-08-01")
    msg = health.alert_delta(degraded("source rentcast: 403", "detail: 400 Bad Request"),
                             state, today="2026-08-02")
    assert msg and "400" in msg
    assert "403" not in msg, "the already-reported problem must not be repeated"


def test_resolution_is_announced_not_silent():
    state = {}
    health.alert_delta(degraded("source rentcast: 403"), state, today="2026-08-01")
    msg = health.alert_delta({"verdict": health.OK, "problems": []}, state, today="2026-08-02")
    assert msg and "Resolved:" in msg and "403" in msg
    assert health.alert_delta({"verdict": health.OK, "problems": []}, state,
                              today="2026-08-03") is None
    assert state["health_alerts"] == {}, "resolved problems leave the memory"


def test_a_stale_problem_gets_a_weekly_reminder_then_requiets():
    state = {}
    p = degraded("source rentcast: 403")
    health.alert_delta(p, state, today="2026-08-01")
    assert health.alert_delta(p, state, today="2026-08-07") is None      # day 6: quiet
    msg = health.alert_delta(p, state, today="2026-08-08")               # day 7: nudge
    assert msg and "Still broken" in msg and "since 2026-08-01" in msg
    assert health.alert_delta(p, state, today="2026-08-09") is None      # clock reset


def test_url_noise_does_not_reopen_a_known_problem():
    state = {}
    health.alert_delta(degraded("rentcast: 403 for url: https://x.io/a?zip=10304"),
                       state, today="2026-08-01")
    assert health.alert_delta(degraded("rentcast: 403 for url: https://x.io/a?zip=10312"),
                              state, today="2026-08-02") is None


def test_a_failed_run_headline_says_so():
    msg = health.alert_delta({"verdict": health.FAILED, "problems": ["run aborted: x"]},
                             {}, today="2026-08-01")
    assert "FAILED" in msg


def test_alert_memory_survives_the_state_file_round_trip():
    p = os.path.join(tempfile.mkdtemp(), "state.json")
    state = {}
    health.alert_delta(degraded("source rentcast: 403"), state, today="2026-08-01")
    schedule.save(p, state)
    reloaded = schedule.load(p)
    assert health.alert_delta(degraded("source rentcast: 403"), reloaded,
                              today="2026-08-02") is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
