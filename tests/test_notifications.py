#!/usr/bin/env python3
"""Notification selection and formatting tests; no Telegram request is made."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.access import Access  # noqa: E402
from kash.notifications import (  # noqa: E402
    actionable_listings, format_listing_brief, format_onboarding, format_testing_digest,
    testing_recipients, unsent_actionable_listings,
)
from kash.store import Store  # noqa: E402
from run_update import push_telegram  # noqa: E402

PREFS = {"eligibility": {"telegram_min_baths": 2.5}}


def _listing(**changes):
    record = {
        "street_address": "12 Example Street", "zip": "10308", "list_price": 700000,
        "property_type": "sf_detached", "baths": "2.5", "flood_zone": "X",
        "listing_url": "https://example.com/12-example", "status": "active",
    }
    record.update(changes)
    return record


def test_allowed_recipients_excludes_pending_and_denied_accounts():
    store = Store(":memory:")
    access = Access(store)
    access.add(101, "Allowed")
    access.add(202, "Pending", status="pending")
    access.add(303, "Denied", status="denied")
    assert access.allowed_ids() == [101]
    store.close()


def test_explicit_testing_recipient_list_is_limited_to_allowed_accounts():
    prefs = {"telegram_recipient_ids": [7512954760, 5143942438]}
    assert testing_recipients([5143942438, 5584430379, 7512954760], prefs) == [7512954760, 5143942438]
    assert testing_recipients([5143942438], prefs) == [5143942438]


def test_onboarding_delivery_state_is_per_recipient():
    store = Store(":memory:")
    assert store.notification_sent(101, "onboarding") is False
    store.mark_notification_sent(101, "onboarding")
    assert store.notification_sent(101, "onboarding") is True
    assert store.notification_sent(202, "onboarding") is False
    store.close()


def test_delayed_flood_enrichment_can_alert_once_when_listing_becomes_safe():
    store = Store(":memory:")
    store.upsert(_listing(flood_zone=None), "fixture")
    key = store.conn.execute("SELECT match_key FROM listings").fetchone()[0]
    assert unsent_actionable_listings(store, 101, PREFS) == []
    store.update_fields(key, {"flood_zone": "X"})
    assert [row["street_address"] for row in unsent_actionable_listings(store, 101, PREFS)] == ["12 Example Street"]
    store.mark_notification_sent(101, f"listing:{key}")
    assert unsent_actionable_listings(store, 101, PREFS) == []
    store.close()


def test_actionable_listings_need_alert_policy_and_clickable_link():
    selected = actionable_listings([
        _listing(), _listing(baths="2"), _listing(flood_zone="AE"), _listing(listing_url=None),
    ], PREFS)
    assert selected == [_listing()]


def test_actionable_listings_require_absolute_https_link():
    assert actionable_listings([_listing(listing_url="javascript:alert(1)")], PREFS) == []
    assert actionable_listings([_listing(listing_url="http://example.com/listing")], PREFS) == []


def test_testing_digest_contains_a_clickable_link_and_onboarding_is_plain_language():
    digest = format_testing_digest([_listing()])
    assert "https://example.com/12-example" in digest
    onboarding = format_onboarding()
    assert "Robo Kash" in onboarding
    assert "read-only" in onboarding.lower()
    assert "two-way Telegram" in onboarding


def test_listing_brief_is_deterministic_factual_and_lighthearted():
    brief = format_listing_brief(_listing(
        beds=4,
        listing_description="Lower-level studio with separate entrance and second kitchen.",
    ))
    assert "12 Example Street" in brief
    assert "$700,000" in brief
    assert "4 bd · 2.5 ba" in brief
    assert "separate entrance" in brief.lower()
    assert "second kitchen" in brief.lower()
    assert "https://example.com/12-example" in brief
    assert format_listing_brief(_listing()) == format_listing_brief(_listing())


def test_listing_brief_uses_robo_kash_and_occasional_non_targeted_street_tone():
    brief = format_listing_brief(_listing())
    assert "Robo Kash" in brief
    assert "no bullshit" in brief.lower()


def test_telegram_sender_targets_each_allowed_recipient_without_live_request():
    class Response:
        @staticmethod
        def json():
            return {"ok": True}

    calls = []
    old_token = os.environ.get("KASH_BOT_TOKEN")
    os.environ["KASH_BOT_TOKEN"] = "test-token"
    try:
        result = push_telegram("hello", [101, 202], request_get=lambda _url, **kwargs: (calls.append(kwargs), Response())[1])
    finally:
        if old_token is None:
            del os.environ["KASH_BOT_TOKEN"]
        else:
            os.environ["KASH_BOT_TOKEN"] = old_token
    assert result == {101: "sent", 202: "sent"}
    assert [call["params"]["chat_id"] for call in calls] == [101, 202]


if __name__ == "__main__":
    test_allowed_recipients_excludes_pending_and_denied_accounts()
    test_explicit_testing_recipient_list_is_limited_to_allowed_accounts()
    test_onboarding_delivery_state_is_per_recipient()
    test_delayed_flood_enrichment_can_alert_once_when_listing_becomes_safe()
    test_actionable_listings_need_alert_policy_and_clickable_link()
    test_actionable_listings_require_absolute_https_link()
    test_testing_digest_contains_a_clickable_link_and_onboarding_is_plain_language()
    test_listing_brief_is_deterministic_factual_and_lighthearted()
    test_listing_brief_uses_robo_kash_and_occasional_non_targeted_street_tone()
    test_telegram_sender_targets_each_allowed_recipient_without_live_request()
    print("PASS — Telegram delivery selects allowed humans and actionable linked listings")
