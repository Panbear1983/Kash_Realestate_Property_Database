#!/usr/bin/env python3
"""Synthesized listing URLs for sources that provide none (RentCast).

The two failure modes that matter: a synthesized URL reaching the detail actor (which
rejects address URLs — the poisoned-batch bug), and the fill fighting the merge (clobbering
a real zpid URL, or being clobbered back to NULL by the next RentCast run). No network.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import urlfill  # noqa: E402
from kash.dedup import match_key  # noqa: E402
from kash.enrich.detail import _zpid  # noqa: E402
from kash.notifications import _is_clickable_url  # noqa: E402
from kash.store import Store  # noqa: E402

PREFS = {"eligibility": {"excluded_property_types": ["condo", "apartment", "lot", "land"],
                         "store_min_baths": 2, "telegram_min_baths": 2.5,
                         "telegram_max_price": 800000}}


def row(addr="45 Fairlawn Loop", **over):
    r = {"street_address": addr, "zip": "10308", "list_price": 700000, "beds": "3",
         "baths": "2.5", "property_type": "sf_detached", "status": "active",
         "listing_url": None}
    r.update(over)
    return r


def store_with(*rows):
    s = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    for r in rows:
        s.upsert(r, source="rentcast")
    return s


# --- the URL itself -------------------------------------------------------------------------

def test_slug_matches_the_pools_curated_style():
    url = urlfill.synthesize_url("45 Fairlawn Loop", "10308")
    assert url == "https://www.zillow.com/homes/45-Fairlawn-Loop-Staten-Island-NY-10308/"


def test_punctuation_and_spacing_collapse_into_single_hyphens():
    url = urlfill.synthesize_url("  85 E. Freedom Ave,  Apt A ", "10306")
    assert url == "https://www.zillow.com/homes/85-E-Freedom-Ave-Apt-A-Staten-Island-NY-10306/"


def test_missing_address_or_zip_yields_none():
    assert urlfill.synthesize_url(None, "10308") is None
    assert urlfill.synthesize_url("45 Fairlawn Loop", "") is None
    assert urlfill.synthesize_url("...", "10308") is None


def test_the_detail_actor_never_receives_a_synthesized_url():
    """The actor rejects address URLs; one in a batch poisons the whole run."""
    url = urlfill.synthesize_url("45 Fairlawn Loop", "10308")
    assert _zpid(url) is None


def test_telegram_accepts_the_synthesized_url():
    assert _is_clickable_url(urlfill.synthesize_url("45 Fairlawn Loop", "10308"))


# --- filling --------------------------------------------------------------------------------

def test_urlless_active_rows_are_filled_and_logged_once():
    s = store_with(row())
    stats = urlfill.fill_missing_urls(s, PREFS)
    assert stats == {"filled": 1, "skipped": 0}
    stored = s.get(match_key(row()))
    assert stored["listing_url"].endswith("45-Fairlawn-Loop-Staten-Island-NY-10308/")
    events = [e for e in s.recent_changes(10) if e["event"] == "url_synthesized"]
    assert len(events) == 1 and "45 Fairlawn Loop" in events[0]["detail"]


def test_filling_is_idempotent():
    s = store_with(row())
    urlfill.fill_missing_urls(s, PREFS)
    assert urlfill.fill_missing_urls(s, PREFS) == {"filled": 0, "skipped": 0}
    assert len([e for e in s.recent_changes(10) if e["event"] == "url_synthesized"]) == 1


def test_policy_excluded_rows_are_skipped_not_dressed_up():
    """A land lot can never alert; a link would only disguise an out-of-policy row."""
    s = store_with(row(property_type="land", baths=None))
    stats = urlfill.fill_missing_urls(s, PREFS)
    assert stats == {"filled": 0, "skipped": 1}
    assert s.get(match_key(row()))["listing_url"] is None


def test_rows_with_a_real_url_are_untouched():
    real = "https://www.zillow.com/homedetails/x/123_zpid/"
    s = store_with(row(listing_url=real))
    assert urlfill.fill_missing_urls(s, PREFS) == {"filled": 0, "skipped": 0}
    assert s.get(match_key(row()))["listing_url"] == real


def test_off_market_rows_are_left_alone():
    s = store_with(row(status="sold"))
    assert urlfill.fill_missing_urls(s, PREFS)["filled"] == 0


# --- merge interactions ---------------------------------------------------------------------

def test_a_later_zillow_zpid_url_overwrites_the_synthesized_one():
    s = store_with(row())
    urlfill.fill_missing_urls(s, PREFS)
    real = "https://www.zillow.com/homedetails/45-Fairlawn-Loop/789_zpid/"
    s.upsert(row(listing_url=real), source="zillow")
    assert s.get(match_key(row()))["listing_url"] == real


def test_the_next_rentcast_run_cannot_clobber_the_fill_back_to_null():
    s = store_with(row())
    urlfill.fill_missing_urls(s, PREFS)
    s.upsert(row(listing_url=None), source="rentcast")   # weekly re-sighting, still no URL
    assert s.get(match_key(row()))["listing_url"] is not None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
