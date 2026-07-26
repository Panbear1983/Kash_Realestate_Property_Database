#!/usr/bin/env python3
"""Enrichment ledger: attempt tracking, backoff, and resolution. Temp DB, no network."""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.ledger import BACKOFF_HOURS, Ledger  # noqa: E402
from kash.store import Store                    # noqa: E402

KEY = "addr:1 test ave|10308"


def fresh():
    return Ledger(Store(os.path.join(tempfile.mkdtemp(), "t.db")))


# --- basics ---------------------------------------------------------------------------------

def test_unknown_pair_is_due():
    assert fresh().is_due(KEY, "flood_zone") is True


def test_failure_records_attempt_and_error():
    lg = fresh()
    lg.record_failure(KEY, "flood_zone", "SSLEOFError")
    e = lg.entry(KEY, "flood_zone")
    assert e["attempts"] == 1
    assert "SSLEOFError" in e["last_error"]
    assert e["resolved_at"] is None


def test_success_resolves_and_stops_further_work():
    lg = fresh()
    lg.record_failure(KEY, "flood_zone", "boom")
    lg.record_success(KEY, "flood_zone")
    assert lg.entry(KEY, "flood_zone")["resolved_at"]
    assert lg.is_due(KEY, "flood_zone") is False


def test_attempts_accumulate_across_failures():
    lg = fresh()
    for _ in range(3):
        lg.record_failure(KEY, "flood_zone", "boom")
    assert lg.entry(KEY, "flood_zone")["attempts"] == 3


def test_first_attempt_is_preserved():
    lg = fresh()
    lg.record_failure(KEY, "flood_zone", "a")
    first = lg.entry(KEY, "flood_zone")["first_attempt"]
    lg.record_failure(KEY, "flood_zone", "b")
    assert lg.entry(KEY, "flood_zone")["first_attempt"] == first


def test_fields_are_tracked_independently():
    lg = fresh()
    lg.record_failure(KEY, "flood_zone", "boom")
    assert lg.is_due(KEY, "neighborhood") is True
    assert lg.entry(KEY, "neighborhood") is None


# --- backoff --------------------------------------------------------------------------------

def test_backoff_lengthens_with_attempts():
    lg = fresh()
    lg.record_failure(KEY, "flood_zone", "boom")          # attempts=1 -> wait 1h
    last = datetime.fromisoformat(lg.entry(KEY, "flood_zone")["last_attempt"])
    assert lg.is_due(KEY, "flood_zone", now=last + timedelta(minutes=30)) is False
    assert lg.is_due(KEY, "flood_zone", now=last + timedelta(hours=2)) is True


def test_backoff_reaches_the_long_interval():
    lg = fresh()
    for _ in range(len(BACKOFF_HOURS) + 2):
        lg.record_failure(KEY, "flood_zone", "boom")
    last = datetime.fromisoformat(lg.entry(KEY, "flood_zone")["last_attempt"])
    assert lg.is_due(KEY, "flood_zone", now=last + timedelta(hours=12)) is False
    assert lg.is_due(KEY, "flood_zone", now=last + timedelta(hours=25)) is True


def test_a_dead_source_is_not_retried_every_run():
    """The behaviour this exists to prevent: hammering a dead endpoint on every cycle."""
    lg = fresh()
    lg.record_failure(KEY, "flood_zone", "SSLEOFError")
    last = datetime.fromisoformat(lg.entry(KEY, "flood_zone")["last_attempt"])
    attempts_in_a_day = sum(
        1 for h in range(24) if lg.is_due(KEY, "flood_zone", now=last + timedelta(hours=h)))
    assert attempts_in_a_day < 24, "backoff must reduce attempts below once per hour"


# --- skips ----------------------------------------------------------------------------------

def test_skip_does_not_consume_an_attempt():
    """A row waiting on its coordinates should not be pushed into backoff for that."""
    lg = fresh()
    lg.record_skip(KEY, "flood_zone", "no coordinates")
    e = lg.entry(KEY, "flood_zone")
    assert e["attempts"] == 0
    assert e["last_status"] == "skipped"
    assert lg.is_due(KEY, "flood_zone") is True


# --- reporting ------------------------------------------------------------------------------

def test_unresolved_lists_only_open_entries():
    lg = fresh()
    lg.record_failure("a", "flood_zone", "boom")
    lg.record_failure("b", "flood_zone", "boom")
    lg.record_success("c", "flood_zone")
    keys = {e["match_key"] for e in lg.unresolved("flood_zone")}
    assert keys == {"a", "b"}


def test_unresolved_can_filter_by_field():
    lg = fresh()
    lg.record_failure("a", "flood_zone", "boom")
    lg.record_failure("a", "neighborhood", "boom")
    assert len(lg.unresolved("flood_zone")) == 1
    assert len(lg.unresolved()) == 2


def test_summary_rolls_up_per_field_with_the_last_error():
    lg = fresh()
    for k in ("a", "b", "c"):
        lg.record_failure(k, "flood_zone", "SSLEOFError: handshake")
    lg.record_success("d", "flood_zone")
    s = {x["field"]: x for x in lg.summary()}["flood_zone"]
    assert s["outstanding"] == 3 and s["resolved"] == 1
    assert "SSLEOFError" in s["last_error"]


def test_table_survives_reopening_the_store():
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    Ledger(Store(db)).record_failure(KEY, "flood_zone", "boom")
    assert Ledger(Store(db)).entry(KEY, "flood_zone")["attempts"] == 1


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
