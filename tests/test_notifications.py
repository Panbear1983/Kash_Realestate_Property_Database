#!/usr/bin/env python3
"""Notification selection and formatting tests; no Telegram request is made."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.access import Access  # noqa: E402
from kash.notifications import (  # noqa: E402
    actionable_listings, format_listing_brief, format_onboarding, format_testing_digest,
    health_recipients, listing_delivery_kind, testing_recipients, unsent_actionable_listings,
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


def test_health_alerts_go_only_to_configured_owner_recipient():
    prefs = {
        "telegram_recipient_ids": [7512954760, 5143942438],
        "telegram_health_recipient_ids": [7512954760],
    }
    assert health_recipients([5143942438, 7512954760], prefs) == [7512954760]


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
    pending = unsent_actionable_listings(store, 101, PREFS)
    assert [row["street_address"] for row in pending] == ["12 Example Street"]
    # Ask the module for the key rather than rebuilding it here — the format now includes the
    # price so a re-priced home re-alerts, and a hand-built key silently stops matching.
    store.mark_notification_sent(101, listing_delivery_kind(pending[0]))
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


def test_listing_brief_carries_no_boilerplate():
    """Replaces an earlier test that REQUIRED the 'Robo Kash' prefix and the 'no bullshit'
    sign-off. Measured on the live queue, those plus the generic fallback were 52% of the
    entire message and identical on all 18 listings, so the requirement was withdrawn."""
    brief = format_listing_brief(_listing())
    assert "Robo Kash:" not in brief
    assert "no bullshit" not in brief.lower()
    assert "Quick take:" not in brief


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
    # Discover tests rather than listing them: the hardcoded list silently went stale the
    # moment a test was renamed, and a NameError is a worse failure than a missed test.
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — Telegram delivery selects allowed humans and linked listings")
