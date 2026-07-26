#!/usr/bin/env python3
"""Telegram delivery: nothing is marked delivered that the buyer did not actually receive.

Three silent failures this locks down, all observed on the live 26 July run:
  - notifications.py capped rendering at rows[:20], dropping 8 of 28 actionable listings
  - run_update.py truncated the message at text[:4000] against Telegram's 4096 limit
  - every actionable listing was then marked delivered, so the dropped ones could never
    be shown again

No network: the sender is a fake.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.notifications import (  # noqa: E402
    TELEGRAM_LIMIT, chunk_digest, format_listing_brief, listing_delivery_kind,
    unsent_actionable_listings)
from kash.store import Store  # noqa: E402

PREFS = {"eligibility": {"telegram_min_baths": 2.5, "store_min_baths": 2,
                         "telegram_max_price": 800000, "safe_flood_zones": ["X"],
                         "excluded_property_types": ["condo"]}}


def listing(i, **over):
    r = {"street_address": f"{i} Example Street", "zip": "10308", "list_price": 700000,
         "property_type": "sf_detached", "beds": "3", "baths": "2.5", "flood_zone": "X",
         "listing_url": f"https://example.com/{i}", "status": "active",
         "listing_description": "Bright kitchen and a private yard."}
    r.update(over)
    return r


def store_with(n):
    s = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    for i in range(n):
        s.upsert(listing(i), source="test")
    return s


class FakeSender:
    """Stands in for push_telegram. fail_on is a set of 0-based call indices that fail."""

    def __init__(self, fail_on=(), limit=4096):
        self.fail_on, self.limit = set(fail_on), limit
        self.sent, self.calls = [], 0

    def __call__(self, text, chat_ids):
        i, self.calls = self.calls, self.calls + 1
        assert len(text) <= self.limit, f"message {i} is {len(text)} chars, over {self.limit}"
        if i in self.fail_on:
            return {cid: "Bad Request" for cid in chat_ids}
        self.sent.append(text)
        return {cid: "sent" for cid in chat_ids}


# --- chunking -------------------------------------------------------------------------------

def test_every_listing_appears_in_some_chunk():
    rows = [listing(i) for i in range(25)]
    chunks = chunk_digest(rows)
    covered = [r for _, rs in chunks for r in rs]
    assert len(covered) == 25, f"{len(covered)} of 25 listings made it into a message"
    assert {r["street_address"] for r in covered} == {r["street_address"] for r in rows}


def test_no_chunk_exceeds_the_telegram_limit():
    for text, _ in chunk_digest([listing(i) for i in range(40)]):
        assert len(text) <= 4096, f"chunk is {len(text)} chars"


def test_a_small_batch_is_a_single_message():
    assert len(chunk_digest([listing(i) for i in range(3)])) == 1


def test_no_rows_means_no_messages():
    assert chunk_digest([]) == []


def test_multi_part_messages_are_numbered():
    chunks = chunk_digest([listing(i) for i in range(40)])
    assert len(chunks) > 1
    assert "(1/" in chunks[0][0]


def test_a_single_oversized_listing_still_ships():
    """A pathological description must not silently vanish."""
    chunks = chunk_digest([listing(0, listing_description="x" * 9000)])
    assert len(chunks) == 1 and len(chunks[0][1]) == 1


# --- delivery accounting --------------------------------------------------------------------

def _deliver(store, rows, recipient, sender):
    """Mirrors run_update's loop: mark only the rows in a chunk that actually sent."""
    for text, chunk_rows in chunk_digest(rows):
        if sender(text, [recipient])[recipient] != "sent":
            continue
        for r in chunk_rows:
            k = listing_delivery_kind(r)
            if k:
                store.mark_notification_sent(recipient, k)


def test_a_failed_chunk_leaves_its_listings_unsent():
    """The regression: a partial send must not mark the whole batch delivered."""
    s = store_with(30)
    rows = unsent_actionable_listings(s, 1, PREFS)
    sender = FakeSender(fail_on=(1,))
    _deliver(s, rows, 1, sender)
    remaining = unsent_actionable_listings(s, 1, PREFS)
    assert remaining, "the failed chunk's listings must remain queued for the next run"
    assert len(remaining) < len(rows), "the successful chunk should have been recorded"


def test_a_full_success_clears_the_queue():
    s = store_with(30)
    rows = unsent_actionable_listings(s, 1, PREFS)
    _deliver(s, rows, 1, FakeSender())
    assert unsent_actionable_listings(s, 1, PREFS) == []


def test_recipients_are_tracked_independently():
    s = store_with(5)
    _deliver(s, unsent_actionable_listings(s, 1, PREFS), 1, FakeSender())
    assert unsent_actionable_listings(s, 1, PREFS) == []
    assert len(unsent_actionable_listings(s, 2, PREFS)) == 5


# --- re-alerting on a price change ----------------------------------------------------------

def test_a_price_change_produces_a_new_delivery_kind():
    """A price drop on a home you have already seen is the most actionable event there is."""
    a = listing_delivery_kind(listing(1, list_price=700000))
    b = listing_delivery_kind(listing(1, list_price=679000))
    assert a != b


def test_an_unchanged_price_is_not_re_sent():
    s = store_with(1)
    _deliver(s, unsent_actionable_listings(s, 1, PREFS), 1, FakeSender())
    assert unsent_actionable_listings(s, 1, PREFS) == []


def test_a_repriced_home_re_alerts():
    s = store_with(1)
    _deliver(s, unsent_actionable_listings(s, 1, PREFS), 1, FakeSender())
    s.upsert(listing(0, list_price=679000), source="test")
    assert len(unsent_actionable_listings(s, 1, PREFS)) == 1


def test_a_listing_without_a_price_still_gets_a_kind():
    assert listing_delivery_kind(listing(1, list_price=None)) is not None


# --- brief content --------------------------------------------------------------------------

def test_extracted_multigen_signal_reaches_telegram():
    """A home the ranker flags TOP PRIORITY must not arrive saying 'no extra feature claims'."""
    row = listing(1, listing_description="Private side entry to a finished lower level.",
                  signal_extracted_at="2026-07-27", signal_multigenerational=True,
                  signal_separate_entrance=True, signal_evidence="private side entry")
    text = format_listing_brief(row)
    assert "separate entrance" in text
    assert "no extra feature claims" not in text


def test_multigen_without_a_separate_entrance_still_reads_as_multigen():
    row = listing(1, listing_description="Two family detached home.",
                  signal_extracted_at="2026-07-27", signal_multigenerational=True,
                  signal_separate_entrance=False, signal_evidence="Two family detached home")
    assert "multigenerational" in format_listing_brief(row)


def test_the_keyword_path_still_works_without_extraction():
    row = listing(1, listing_description="Home with a separate entrance to the lower level.")
    assert "separate entrance" in format_listing_brief(row)


def test_a_plain_listing_gets_the_fallback():
    assert "no extra feature claims" in format_listing_brief(listing(1))


def test_limit_leaves_headroom_under_telegram_maximum():
    assert TELEGRAM_LIMIT < 4096


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
