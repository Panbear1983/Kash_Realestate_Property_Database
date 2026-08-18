#!/usr/bin/env python3
"""Weekly market pulse: computed from the changelog + pool, windowed, human. No network."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import pulse  # noqa: E402
from kash.dedup import match_key  # noqa: E402
from kash.store import Store  # noqa: E402

PREFS = {"price": {"min": 560915, "max": 900000},
         "eligibility": {"telegram_max_price": 800000}}


def _store():
    s = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    for i, price in ((1, 600000), (2, 700000), (3, 850000)):
        s.upsert({"street_address": f"{i} Pulse St", "zip": "10308", "list_price": price,
                  "beds": "3", "baths": "2", "property_type": "sf_detached",
                  "status": "active", "sqft": 1500}, source="test")
    # Seeding writes new_listing events stamped with the REAL today; push them far outside
    # every test window so each test controls its own event set.
    s.conn.execute("UPDATE changelog SET ts='2026-07-01'")
    s.conn.commit()
    return s


def _log_at(s, ts, event, detail, key="addr:1 pulse st|10308"):
    s.conn.execute("INSERT INTO changelog (match_key,event,detail,source,ts) VALUES (?,?,?,?,?)",
                   (key, event, detail, "test", ts))
    s.conn.commit()


def test_the_window_is_respected():
    s = _store()
    _log_at(s, "2026-08-15", "new_listing", "1 Pulse St @ 600000")
    _log_at(s, "2026-08-01", "new_listing", "old event outside the window")
    p = pulse.compute(s, PREFS, days=7, today="2026-08-18")
    assert p["new"] == 1, "only events inside the window count"


def test_price_cuts_carry_addresses_and_sizes():
    s = _store()
    _log_at(s, "2026-08-16", "price_drop", "700000 -> 650000")
    p = pulse.compute(s, PREFS, days=7, today="2026-08-18")
    assert p["drops"] == 1
    assert p["top_cuts"] and "1 Pulse St" in p["top_cuts"][0]
    assert "−$50,000" in p["top_cuts"][0]
    assert "Biggest cuts:" in pulse.format_pulse(p)


def test_the_watchlist_counts_homes_above_the_alert_cap():
    p = pulse.compute(_store(), PREFS, days=7, today="2026-08-18")
    assert p["stretch_count"] == 1              # the 850k row
    assert "above your $800,000 alert cap" in pulse.format_pulse(p)


def test_gone_and_back_are_counted():
    s = _store()
    _log_at(s, "2026-08-16", "status_change", "active -> off_market")
    _log_at(s, "2026-08-17", "status_change", "off_market -> active")
    p = pulse.compute(s, PREFS, days=7, today="2026-08-18")
    assert p["gone"] == 1 and p["back"] == 1


def test_a_quiet_week_says_so():
    p = pulse.compute(_store(), PREFS, days=7, today="2026-08-18")
    assert "A quiet week." in pulse.format_pulse(p)


def test_medians_come_from_the_in_band_active_pool():
    p = pulse.compute(_store(), PREFS, days=7, today="2026-08-18")
    assert p["in_band"] == 3 and p["median_price"] == 700000
    assert p["median_psf"], "sqft rows produce a $/sqft median"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
