#!/usr/bin/env python3
"""Ageing listings out of the pool — and, mostly, refusing to.

The expensive failure mode is a false positive: burying a house that is still for sale
because a capped search happened not to return it. These tests pin the conditions under
which absence counts as evidence at all. No network.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import lifecycle  # noqa: E402
from kash.dedup import match_key  # noqa: E402
from kash.store import Store  # noqa: E402

PREFS = {"lifecycle": {"enabled": True, "misses_before_off_market": 2}}

COVERAGE = {"source": "zillow", "price_min": 560915, "price_max": 640915,
            "beds_min": 3, "baths_min": 2, "excluded_types": ["condo", "apartment", "lot"],
            "zips": ["10304", "10308"], "results_limit": 40, "truncated": False}


def row(addr="1 Test Ave", **over):
    r = {"street_address": addr, "zip": "10308", "list_price": 600000, "beds": "3",
         "baths": "2", "property_type": "sf_detached", "status": "active",
         "listing_url": "https://www.zillow.com/homedetails/x/1_zpid/"}
    r.update(over)
    return r


def store_with(*rows, source="zillow"):
    s = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    for r in rows:
        s.upsert(r, source=source)
    return s


def key_of(r):
    return match_key(r)


def status_of(store, r):
    return store.get(key_of(r))["status"]


def age(store, seen=(), coverages=(COVERAGE,), prefs=PREFS, today="2026-08-20"):
    return lifecycle.age_listings(store, prefs, list(coverages), list(seen), today=today)


# --- what counts as evidence ----------------------------------------------------------------

def test_a_truncated_search_ages_nothing():
    """A run that hit its cap was cut off mid-list — listing 41 was never in the answer."""
    r = row()
    s = store_with(r)
    capped = {**COVERAGE, "truncated": True}
    for day in ("2026-08-20", "2026-08-21", "2026-08-22"):
        stats = age(s, seen=[], coverages=[capped], today=day)
    assert stats["aged"] == 0 and stats["covered"] == 0
    assert stats["truncated_runs"] == 1
    assert status_of(s, r) == "active"


def test_two_misses_under_the_cap_age_the_row():
    r = row()
    s = store_with(r)
    s.update_fields(key_of(r), {"fetched_at": "2026-08-01"})     # not seen today
    first = age(s, seen=[], today="2026-08-20")
    assert first["missed"] == 1 and first["aged"] == 0
    assert status_of(s, r) == "active", "one absence is not enough"
    second = age(s, seen=[], today="2026-08-23")
    assert second["aged"] == 1
    assert status_of(s, r) == "off_market"


def test_misses_must_be_consecutive():
    r = row()
    s = store_with(r)
    s.update_fields(key_of(r), {"fetched_at": "2026-08-01"})
    age(s, seen=[], today="2026-08-20")
    age(s, seen=[key_of(r)], today="2026-08-21")                 # turned up again
    stats = age(s, seen=[], today="2026-08-22")
    assert stats["aged"] == 0 and status_of(s, r) == "active"
    assert lifecycle.Lifecycle(s).misses(key_of(r)) == 1


def test_a_row_seen_today_by_any_source_is_not_a_miss():
    r = row()
    s = store_with(r)          # upsert stamps fetched_at = today
    stats = age(s, seen=[], today=s.get(key_of(r))["fetched_at"])
    assert stats["missed"] == 0


# --- who is off limits ----------------------------------------------------------------------

def test_curated_rows_are_never_aged():
    """An imported row has no live sighting to miss, so absence says nothing about it."""
    r = row()
    s = store_with(r, source="seed:docx")
    for day in ("2026-08-20", "2026-08-23", "2026-08-26"):
        stats = age(s, seen=[], today=day)
    assert stats["covered"] == 0 and status_of(s, r) == "active"


def test_a_house_the_buyer_is_working_is_never_aged():
    for engaged in ({"favorite": True}, {"viewing_status": "scheduled"},
                    {"offer_status": "offered"}, {"contacted_agent": True}):
        r = row()
        s = store_with(r)
        s.update_fields(key_of(r), {"fetched_at": "2026-08-01", **engaged},
                        allow_protected=True)
        for day in ("2026-08-20", "2026-08-23"):
            age(s, seen=[], today=day)
        assert status_of(s, r) == "active", f"{engaged} must be left alone"


def test_pending_and_sold_rows_are_left_alone():
    for st in ("pending", "sold", "off_market", "attorney_review"):
        r = row(status=st)
        s = store_with(r)
        s.update_fields(key_of(r), {"fetched_at": "2026-08-01"})
        for day in ("2026-08-20", "2026-08-23"):
            age(s, seen=[], today=day)
        assert status_of(s, r) == st


# --- coverage boundaries --------------------------------------------------------------------

def test_rows_outside_the_searched_band_are_not_covered():
    r = row(list_price=780000)          # above this run's band
    s = store_with(r)
    s.update_fields(key_of(r), {"fetched_at": "2026-08-01"})
    for day in ("2026-08-20", "2026-08-23"):
        stats = age(s, seen=[], today=day)
    assert stats["covered"] == 0 and status_of(s, r) == "active"


def test_a_row_that_cannot_answer_the_query_is_not_covered():
    """No price / no beds means we cannot say it would have been in the result set."""
    for blank in ({"list_price": None}, {"beds": None}, {"baths": None},
                  {"property_type": None}):
        r = row(**blank)
        s = store_with(r)
        s.update_fields(key_of(r), {"fetched_at": "2026-08-01"})
        stats = age(s, seen=[], today="2026-08-20")
        assert stats["covered"] == 0, f"{blank} must not be aged on a filtered search"


def test_bed_ranges_are_read_on_their_low_end():
    assert lifecycle.in_coverage(row(beds="3-4", source="zillow"), COVERAGE) is True
    assert lifecycle.in_coverage(row(beds="2-3", source="zillow"), COVERAGE) is False


def test_a_zip_outside_the_allow_list_is_not_covered():
    assert lifecycle.in_coverage(row(zip="10301", source="zillow"), COVERAGE) is False


def test_another_providers_row_is_not_aged_by_this_search():
    r = row()
    s = store_with(r, source="rentcast")
    s.update_fields(key_of(r), {"fetched_at": "2026-08-01"})
    for day in ("2026-08-20", "2026-08-23"):
        stats = age(s, seen=[], today=day)
    assert stats["covered"] == 0 and status_of(s, r) == "active"


# --- reporting and reversal -----------------------------------------------------------------

def test_ageing_is_logged_where_the_digest_reads_it():
    r = row()
    s = store_with(r)
    s.update_fields(key_of(r), {"fetched_at": "2026-08-01"})
    age(s, seen=[], today="2026-08-20")
    age(s, seen=[], today="2026-08-23")
    events = [e for e in s.recent_changes(20) if e["event"] == "status_change"]
    assert events and "-> off_market" in events[0]["detail"], \
        "the digest groups 'gone' listings by this exact wording"


def test_a_relisted_house_comes_back_by_itself():
    r = row()
    s = store_with(r)
    s.update_fields(key_of(r), {"fetched_at": "2026-08-01"})
    age(s, seen=[], today="2026-08-20")
    age(s, seen=[], today="2026-08-23")
    assert status_of(s, r) == "off_market"
    s.upsert(row(), source="zillow")                     # turns up in a later search
    assert status_of(s, r) == "active"
    assert s.get(key_of(r))["times_relisted"] == 1


def test_one_bad_scrape_cannot_bury_the_whole_band():
    """A per-run cap bounds the damage if a search under-returns; the rest wait a run."""
    rows = [row(f"{i} Test Ave") for i in range(14)]
    s = store_with(*rows)
    for r in rows:
        s.update_fields(key_of(r), {"fetched_at": "2026-08-01"})
    age(s, seen=[], today="2026-08-20")
    stats = age(s, seen=[], today="2026-08-23")
    assert stats["aged"] == 10 and stats["deferred"] == 4
    assert sum(status_of(s, r) == "off_market" for r in rows) == 10
    # the held-back ones are not forgotten: they age on the following run
    third = age(s, seen=[], today="2026-08-26")
    assert third["aged"] == 4
    assert all(status_of(s, r) == "off_market" for r in rows)


def test_the_cap_is_configurable():
    rows = [row(f"{i} Test Ave") for i in range(4)]
    s = store_with(*rows)
    for r in rows:
        s.update_fields(key_of(r), {"fetched_at": "2026-08-01"})
    prefs = {"lifecycle": {"misses_before_off_market": 1, "max_aged_per_run": 2}}
    stats = age(s, seen=[], prefs=prefs, today="2026-08-20")
    assert stats["aged"] == 2 and stats["deferred"] == 2


def test_disabled_by_preference_does_nothing():
    r = row()
    s = store_with(r)
    s.update_fields(key_of(r), {"fetched_at": "2026-08-01"})
    stats = age(s, seen=[], prefs={"lifecycle": {"enabled": False}}, today="2026-08-20")
    assert stats == {"skipped": "disabled"} and status_of(s, r) == "active"


def test_no_coverage_at_all_is_reported_not_silent():
    s = store_with(row())
    stats = age(s, seen=[], coverages=[None], today="2026-08-20")
    assert stats["covered"] == 0 and stats["truncated_runs"] == 0, \
        "a source that reports no coverage is not a truncated search"
    assert "no source reported complete coverage" in lifecycle.format_stats(stats)


def test_a_capped_search_says_so_in_the_run_report():
    s = store_with(row())
    stats = age(s, seen=[], coverages=[{**COVERAGE, "truncated": True}], today="2026-08-20")
    assert "hit their result cap" in lifecycle.format_stats(stats)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
