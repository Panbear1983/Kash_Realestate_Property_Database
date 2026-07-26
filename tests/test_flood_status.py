#!/usr/bin/env python3
"""Flood-zone resolution: an unavailable lookup must never read as safe.

Providers are stubbed — no network. The one live probe is opt-in via KASH_LIVE=1.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.enrich import flood  # noqa: E402
from kash.eligibility import classify  # noqa: E402
from kash.notifications import alert_ready  # noqa: E402

PREFS = {"eligibility": {"store_min_baths": 2, "telegram_min_baths": 2.5,
                         "telegram_max_price": 800000, "safe_flood_zones": ["X"],
                         "excluded_property_types": ["condo"]}}


class Stub:
    def __init__(self, name, result=None, raises=None, probe=None):
        self.name = name
        self._result, self._raises = result, raises
        self._probe = probe          # what the coverage probe point returns
        self.calls = 0               # real queries only — probes are not counted

    def query(self, lat, lon, timeout=30):
        if (lat, lon) == flood.PROBE_POINT:
            return self._probe       # may be None, meaning "fails the coverage probe"
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._result


def use(*providers):
    flood.PROVIDERS[:] = list(providers)
    flood.reset_probe_cache()


def setup_function(_):
    flood.reset_probe_cache()


ORIGINAL = list(flood.PROVIDERS)


def teardown_function(_):
    flood.PROVIDERS[:] = ORIGINAL
    flood.reset_probe_cache()


# --- the core guarantee ---------------------------------------------------------------------

def test_unavailable_never_returns_a_zone():
    use(Stub("dead", raises=RuntimeError("boom"), probe="AE"))
    zone, status = flood.flood_zone_status(40.55, -74.15)
    assert status == flood.UNAVAILABLE
    assert zone is None


def test_unavailable_is_not_reported_as_x():
    use(Stub("dead", raises=RuntimeError("boom"), probe="AE"))
    assert flood.flood_zone(40.55, -74.15) is None, "an outage must not read as zone X"


def test_provider_without_coverage_is_skipped_entirely():
    """The old bug: a provider that cannot see Staten Island returning empty meant 'safe'."""
    blind = Stub("blind", result=None, probe=None)   # probe returns None != AE
    use(blind)
    zone, status = flood.flood_zone_status(40.55, -74.15)
    assert status == flood.UNAVAILABLE and zone is None
    assert blind.calls == 0, "must not query a provider that failed its coverage probe"


def test_missing_coordinates_are_unavailable():
    assert flood.flood_zone_status(None, None) == (None, flood.UNAVAILABLE)
    assert flood.flood_zone_status(40.55, None)[1] == flood.UNAVAILABLE


# --- normal resolution ----------------------------------------------------------------------

def test_a_found_zone_resolves():
    use(Stub("good", result="AE", probe="AE"))
    assert flood.flood_zone_status(40.57, -74.09) == ("AE", flood.RESOLVED)


def test_empty_from_a_covering_provider_means_x():
    use(Stub("good", result=None, probe="AE"))
    zone, status = flood.flood_zone_status(40.61, -74.13)
    assert zone == "X" and status == flood.NOT_MAPPED


# --- chain behaviour ------------------------------------------------------------------------

def test_falls_through_to_the_second_provider():
    dead = Stub("dead", raises=RuntimeError("tls"), probe="AE")
    good = Stub("good", result="AE", probe="AE")
    use(dead, good)
    assert flood.flood_zone_status(40.57, -74.09) == ("AE", flood.RESOLVED)


def test_uncovered_first_provider_does_not_block_the_second():
    blind = Stub("blind", result=None, probe=None)
    good = Stub("good", result="VE", probe="AE")
    use(blind, good)
    assert flood.flood_zone_status(40.57, -74.09) == ("VE", flood.RESOLVED)


def test_coverage_probe_is_cached():
    good = Stub("good", result="X", probe="AE")
    use(good)
    for _ in range(4):
        flood.flood_zone_status(40.61, -74.13)
    assert good.calls == 4      # four real queries...
    # ...and the probe is not re-run each time; clearing forces a fresh one
    assert flood._probe_cache.get("good") is not None


# --- the downstream consequence -------------------------------------------------------------

def test_alert_gate_rejects_a_listing_with_no_zone():
    row = {"status": "active", "listing_url": "https://example.com/x",
           "baths": "3", "list_price": 700000, "property_type": "sf_detached",
           "flood_zone": None}
    assert classify(row, PREFS).telegram_eligible is True, "eligible on every axis but flood"
    assert alert_ready(row, PREFS) is False, "a null zone must never pass the alert gate"


def test_alert_gate_accepts_a_confirmed_safe_zone():
    row = {"status": "active", "listing_url": "https://example.com/x",
           "baths": "3", "list_price": 700000, "property_type": "sf_detached",
           "flood_zone": "X"}
    assert alert_ready(row, PREFS) is True


def test_alert_gate_rejects_a_hazard_zone():
    row = {"status": "active", "listing_url": "https://example.com/x",
           "baths": "3", "list_price": 700000, "property_type": "sf_detached",
           "flood_zone": "AE"}
    assert alert_ready(row, PREFS) is False


# --- optional live probe --------------------------------------------------------------------

def test_live_providers(capsys=None):
    """Opt-in: KASH_LIVE=1 reports which providers actually answer right now."""
    if os.environ.get("KASH_LIVE") != "1":
        return
    flood.PROVIDERS[:] = ORIGINAL
    flood.reset_probe_cache()
    for p in flood.PROVIDERS:
        print(f"      {p.name}: covers_si={flood._covers_staten_island(p)}")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        setup_function(None)
        try:
            fn()
        finally:
            teardown_function(None)
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
