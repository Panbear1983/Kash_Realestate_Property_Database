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
    TELEGRAM_HARD_LIMIT, TELEGRAM_LIMIT, chunk_digest, format_listing_brief,
    listing_delivery_kind, split_text, unsent_actionable_listings)
from kash.store import Store  # noqa: E402

PREFS = {"eligibility": {"telegram_min_baths": 2.5, "store_min_baths": 2,
                         "telegram_max_price": 800000, "safe_flood_zones": ["X"],
                         "excluded_property_types": ["condo"]}}


def listing(i, **over):
    # Mirrors a real row: analysis, tier and priority are populated on 18 of 18 live listings,
    # so a fixture without them produces unrealistically short briefs.
    r = {"street_address": f"{i} Example Street", "zip": "10308", "list_price": 700000,
         "property_type": "sf_detached", "beds": "3", "baths": "2.5", "flood_zone": "X",
         "listing_url": f"https://example.com/{i}", "status": "active",
         "sqft": 1800, "neighborhood": "Great Kills", "tier": "B", "view_priority": "soon",
         "analysis": f"RANKED #{i} — solid three-bed in a good school zone, fair price per "
                     f"square foot and no flood exposure worth worrying about.",
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


def test_no_generic_fallback_line_exists():
    """It fired on 18 of 18 queued listings and was 40% of the whole message."""
    text = format_listing_brief(listing(1))
    assert "no extra feature claims" not in text
    assert "no bullshit" not in text


def test_no_boilerplate_prefixes():
    """'Robo Kash:' restates the message header; 'Quick take:' labels the obvious."""
    text = format_listing_brief(listing(1, analysis="RANKED #3 — worth a look."))
    assert "Robo Kash:" not in text
    assert "Quick take:" not in text


def test_the_verdict_appears_in_the_headline():
    text = format_listing_brief(listing(1, tier="A", view_priority="now"))
    assert "[A · now]" in text


def test_the_analysis_reaches_the_message():
    text = format_listing_brief(listing(1, analysis="RANKED #1 — CALL AGENT TODAY."))
    assert "CALL AGENT TODAY" in text


def test_the_auto_prefix_is_stripped():
    assert "[auto]" not in format_listing_brief(listing(1, analysis="[auto] Solid buy."))


def test_a_long_analysis_is_cut_on_a_word_boundary():
    text = format_listing_brief(listing(1, analysis="word " * 200))
    body = [ln for ln in text.split("\n") if ln.startswith("word")][0]
    assert body.endswith("…") and not body.endswith("wor…")


def test_absent_facts_are_omitted_not_rendered_as_gaps():
    """A live message showed '4 ba' with no beds because the gap still emitted a separator."""
    text = format_listing_brief(listing(1, beds=None, sqft=None, monthly_piti=None))
    assert "None" not in text
    assert " ·  · " not in text


def test_a_listing_with_nothing_extra_is_just_three_lines():
    text = format_listing_brief(listing(1, analysis=None))
    assert len(text.split("\n")) == 3


def test_briefs_do_not_repeat_across_a_batch():
    """The check that would have caught the original problem."""
    import collections
    rows = [listing(i, analysis=f"RANKED #{i} — reason {i}.", list_price=700000 + i * 1000,
                    sqft=1500 + i * 10) for i in range(12)]
    lines = collections.Counter(
        ln for r in rows for ln in format_listing_brief(r).split("\n"))
    worst = max(lines.values())
    assert worst <= len(rows) // 3, f"a line repeats {worst} times across {len(rows)} listings"


def test_limit_leaves_headroom_under_telegram_maximum():
    assert TELEGRAM_LIMIT < TELEGRAM_HARD_LIMIT


# --- preamble splitting (the production failure on 27 July) -----------------------------------

def test_a_long_digest_is_split_rather_than_prepended():
    """Live failure: the listing chunk respected the limit, but the digest prepended to it
    pushed the combined message over 4096 and Telegram rejected the whole thing."""
    digest = "\n".join(f"  - {i} Somewhere Ave @ 7{i:05d}" for i in range(300))
    parts = split_text(digest)
    assert len(parts) > 1
    assert all(len(p) <= TELEGRAM_LIMIT for p in parts)


def test_split_text_preserves_every_line():
    digest = "\n".join(f"line {i}" for i in range(500))
    assert sum(p.count("line ") for p in split_text(digest)) == 500


def test_short_text_is_a_single_part():
    assert split_text("just a line") == ["just a line"]


def test_empty_text_yields_nothing():
    assert split_text("") == []
    assert split_text(None) == []


def test_a_single_over_long_line_is_hard_split():
    parts = split_text("x" * 9000)
    assert len(parts) > 1 and all(len(p) <= TELEGRAM_LIMIT for p in parts)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
