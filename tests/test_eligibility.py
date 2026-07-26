#!/usr/bin/env python3
"""Eligibility policy tests; no provider, Telegram, or database required."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import eligibility, pipeline  # noqa: E402

PREFS = {
    "price": {"min": 550000, "max": 1150000},
    "zip_prefixes": ["103"],
    "eligibility": {
        "excluded_property_types": ["condo", "apartment", "lot"],
        "store_min_baths": 2,
        "telegram_min_baths": 2.5,
        "telegram_max_price": 800000,
        "safe_flood_zones": ["X"],
    },
}


def _listing(**changes):
    listing = {
        "street_address": "12 Example Street",
        "zip": "10308",
        "list_price": 700000,
        "property_type": "sf_detached",
        "baths": "3",
    }
    listing.update(changes)
    return listing


def test_two_bath_home_is_admitted_but_not_telegram_eligible():
    decision = eligibility.classify(_listing(baths="2"), PREFS)
    assert decision.admit is True
    assert decision.telegram_eligible is False
    assert "bath_count_below_alert_minimum" in decision.reasons


def test_two_point_five_baths_qualifies_for_telegram_pending_flood():
    decision = eligibility.classify(_listing(baths="2.5 baths"), PREFS)
    assert decision.admit is True
    assert decision.telegram_eligible is True
    assert decision.requires_flood_verification is True


def test_over_800k_listing_is_stored_but_not_telegram_eligible():
    decision = eligibility.classify(_listing(list_price=800001), PREFS)
    assert decision.admit is True
    assert decision.telegram_eligible is False
    assert "list_price_above_alert_maximum" in decision.reasons


def test_alert_readiness_requires_safe_flood_zone():
    assert eligibility.alert_ready(_listing(baths="2.5", flood_zone="X"), PREFS) is True
    assert eligibility.alert_ready(_listing(baths="2.5", flood_zone="AE"), PREFS) is False
    assert eligibility.alert_ready(_listing(baths="2.5", flood_zone="AO"), PREFS) is False
    assert eligibility.alert_ready(_listing(baths="2.5", flood_zone="V"), PREFS) is False
    assert eligibility.alert_ready(_listing(baths="2.5"), PREFS) is False


def test_reliable_excluded_type_is_not_admitted():
    decision = eligibility.classify(_listing(property_type="condo"), PREFS)
    assert decision.admit is False
    assert "excluded_property_type" in decision.reasons


def test_fewer_than_two_baths_are_not_admitted():
    decision = eligibility.classify(_listing(baths="1"), PREFS)
    assert decision.admit is False
    assert "bath_count_below_storage_minimum" in decision.reasons


def test_unparseable_baths_are_retained_but_cannot_notify():
    decision = eligibility.classify(_listing(baths="unknown"), PREFS)
    assert decision.admit is True
    assert decision.telegram_eligible is False
    assert "unparseable_bath_count" in decision.reasons


def test_pipeline_rejects_excluded_type_but_keeps_two_bath_listing():
    assert pipeline._in_scope(_listing(property_type="condo"), PREFS) is False
    assert pipeline._in_scope(_listing(baths="2"), PREFS) is True


if __name__ == "__main__":
    test_two_bath_home_is_admitted_but_not_telegram_eligible()
    test_two_point_five_baths_qualifies_for_telegram_pending_flood()
    test_over_800k_listing_is_stored_but_not_telegram_eligible()
    test_alert_readiness_requires_safe_flood_zone()
    test_reliable_excluded_type_is_not_admitted()
    test_fewer_than_two_baths_are_not_admitted()
    test_unparseable_baths_are_retained_but_cannot_notify()
    test_pipeline_rejects_excluded_type_but_keeps_two_bath_listing()
    print("PASS — admission and Telegram eligibility are separate policies")
