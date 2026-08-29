#!/usr/bin/env python3
"""The spoken morning briefing, and the opt-in rules for sending it.

Two things are being protected here. First, that the audio never states a number the run
did not produce — a spoken briefing is harder to check than a written one, so a parse it
cannot trust must fall silent rather than guess. Second, that voice is genuinely optional:
nobody is spoken to without switching it on, and no audio failure can cost a recipient the
written report or cause it to be re-sent.
"""
import os
import sys
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import run_update  # noqa: E402
from kash import voice_digest  # noqa: E402

TUESDAY = date(2026, 8, 25)


def _events():
    """A realistic run: two losses, four drops, five new — the shape of 2026-08-29."""
    return [
        {"event": "status_change", "detail": "256 Sharpe Ave: active -> off_market (absent)",
         "match_key": "addr:256 sharpe ave|10302"},
        {"event": "status_change", "detail": "128 Lovelace Ave: active -> off_market (absent)",
         "match_key": "addr:128 lovelace ave|10312"},
        {"event": "price_drop", "detail": "875000 -> 850000", "match_key": "addr:35 shawnee st|10301"},
        {"event": "price_drop", "detail": "699997 -> 670000", "match_key": "addr:68 south ave|10303"},
        {"event": "new_listing", "detail": "33 Mosel Loop @ 599900", "match_key": "addr:33 mosel loop|10304"},
        {"event": "new_listing", "detail": "4175 Amboy Rd @ 799000", "match_key": "addr:4175 amboy rd|10308"},
        # Noise the digest ignores; the briefing must ignore it too.
        {"event": "url_synthesized", "detail": "69 Chelsea St: search URL filled",
         "match_key": "addr:69 chelsea st|10307"},
    ]


# --- what gets counted ------------------------------------------------------
def test_events_are_bucketed_the_way_the_written_digest_buckets_them():
    g = voice_digest.summarize(_events())
    assert len(g["new"]) == 2 and len(g["price_drop"]) == 2 and len(g["gone"]) == 2
    assert g["back"] == [], "nothing came back on the market in this run"


def test_bookkeeping_events_are_not_reported_as_market_news():
    g = voice_digest.summarize([{"event": "url_synthesized", "detail": "x", "match_key": "addr:a|1"}])
    assert not any(g.values()), "a filled-in URL is not something to wake someone up for"


def test_a_price_increase_dressed_as_a_drop_is_dropped_not_misreported():
    """Better to say nothing than to announce a cut that did not happen."""
    g = voice_digest.summarize([
        {"event": "price_drop", "detail": "500000 -> 600000", "match_key": "addr:a st|1"}])
    assert g["price_drop"] == []


def test_an_unparseable_detail_never_becomes_a_wrong_number():
    g = voice_digest.summarize([
        {"event": "price_drop", "detail": "reduced a bit", "match_key": "addr:a st|1"}])
    assert g["price_drop"] == []


def test_a_new_listing_with_an_odd_detail_still_counts_using_its_key():
    g = voice_digest.summarize([
        {"event": "new_listing", "detail": "no price here", "match_key": "addr:9 elm rd|10301"}])
    assert len(g["new"]) == 1 and g["new"][0]["address"] == "9 Elm Road"
    assert g["new"][0]["price"] == "", "an unknown price is blank, never invented"


# --- how it reads aloud -----------------------------------------------------
def test_street_abbreviations_are_expanded_so_they_are_not_misread():
    """Spoken, 'st' becomes 'Saint' and 'Rd' gets spelled out letter by letter."""
    assert voice_digest._spoken_address("35 shawnee st") == "35 Shawnee Street"
    assert voice_digest._spoken_address("4175 amboy rd") == "4175 Amboy Road"
    assert voice_digest._spoken_address("256 sharpe ave") == "256 Sharpe Avenue"
    assert voice_digest._spoken_address("68 s bay blvd") == "68 South Bay Boulevard"


def test_prices_are_spoken_as_money_not_as_digit_strings():
    assert voice_digest._price(599900) == "$599,900"
    assert voice_digest._price("670000") == "$670,000"
    assert voice_digest._price(None) == ""


def test_counts_read_as_a_sentence_with_correct_singulars():
    one = voice_digest.summarize([
        {"event": "new_listing", "detail": "1 A St @ 5", "match_key": "addr:1 a st|1"}])
    assert voice_digest._counts_sentence(one) == "1 new listing."
    assert voice_digest._counts_sentence(voice_digest.summarize(_events())) == (
        "2 new listings, 2 price drops, and 2 homes off the market.")


def test_the_briefing_names_the_deepest_cut_not_the_first_one():
    spoken = voice_digest.spoken_briefing(_events(), today=TUESDAY)
    assert "68 South Avenue is down from $699,997 to $670,000" in spoken
    assert "$875,000" not in spoken, "the shallower cut is not the headline"


def test_the_briefing_opens_with_the_day_and_closes_by_pointing_at_the_text():
    spoken = voice_digest.spoken_briefing(_events(), today=TUESDAY)
    assert spoken.startswith("Good morning. Robo Kash here with your Staten Island update for Tuesday.")
    assert spoken.endswith("The full list, with the links, is in the message above.")


def test_the_top_listing_comes_from_the_recipients_own_alert_rows():
    """notifications.unsent_actionable_listings already sorts best-first, so row 0 is
    the home this run ranks highest for this person."""
    listings = [{"street_address": "33 mosel loop", "list_price": 599900},
                {"street_address": "4175 amboy rd", "list_price": 799000}]
    spoken = voice_digest.spoken_briefing(_events(), listings, today=TUESDAY)
    assert "Top of the 2 on your list: 33 Mosel Loop at $599,900." in spoken


def test_the_top_listing_reads_the_pools_own_column_name():
    """The pool column is `street_address`; reading `address` made this line vanish
    silently against real rows while the tests still passed."""
    rows = [{"street_address": "9 elm rd", "list_price": 700000}]
    assert "9 Elm Road" in voice_digest._top_listing_sentence(rows)
    assert voice_digest._top_listing_sentence([{"list_price": 700000}]) == "", \
        "a row with no address says nothing rather than naming a price alone"


def test_no_content_means_no_voice_note_rather_than_an_empty_one():
    assert voice_digest.spoken_briefing([], [], today=TUESDAY) == ""
    assert voice_digest.spoken_briefing(None, None, today=TUESDAY) == ""


def test_listings_alone_are_still_worth_speaking():
    """A quiet night for changes can still surface a home the recipient has not seen."""
    spoken = voice_digest.spoken_briefing([], [{"street_address": "9 elm rd", "list_price": 700000}],
                                          today=TUESDAY)
    assert "Top of your list: 9 Elm Road at $700,000." in spoken


# --- sending is opt-in and never load-bearing -------------------------------
class FakePost:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, url, data=None, files=None, timeout=None):
        self.calls.append((url, data, files))
        payload = {"ok": True, "result": {}} if self.ok else {"ok": False, "description": "Bad Request"}
        return type("R", (), {"json": staticmethod(lambda: payload)})()


def _with_prefs(monkey: dict):
    """Stand in for the bridge: control who has voice on without touching the real file."""
    from kash import voice_link
    voice_link.prefs_for = lambda uid: monkey.get(int(uid), {"enabled": False})
    voice_link.render = lambda text, prefs: b"OGG"
    return voice_link


ON = {"enabled": True, "voice": "Fred", "rate": "+0%", "max_chars": 2000}


def test_a_recipient_who_never_switched_voice_on_is_not_spoken_to():
    _with_prefs({})
    os.environ["KASH_BOT_TOKEN"] = "test-token"
    post = FakePost()
    assert run_update.push_telegram_voice("hello", [11], request_post=post) == {11: "skipped (voice off)"}
    assert post.calls == [], "no audio may be sent to someone who did not ask for it"


def test_only_the_opted_in_recipient_of_a_mixed_pair_gets_audio():
    _with_prefs({11: ON})
    os.environ["KASH_BOT_TOKEN"] = "test-token"
    post = FakePost()
    out = run_update.push_telegram_voice("hello", [11, 22], request_post=post)
    assert out == {11: "sent", 22: "skipped (voice off)"}
    assert len(post.calls) == 1 and post.calls[0][1] == {"chat_id": 11}


def test_a_telegram_rejection_is_reported_not_raised():
    """The written report has already been delivered; audio must not fail the run."""
    _with_prefs({11: ON})
    os.environ["KASH_BOT_TOKEN"] = "test-token"
    assert run_update.push_telegram_voice("hello", [11], request_post=FakePost(ok=False)) == {
        11: "Bad Request"}


def test_a_render_failure_is_reported_not_raised():
    voice_link = _with_prefs({11: ON})
    voice_link.render = lambda text, prefs: None
    os.environ["KASH_BOT_TOKEN"] = "test-token"
    post = FakePost()
    assert run_update.push_telegram_voice("hello", [11], request_post=post) == {
        11: "render unavailable"}
    assert post.calls == []


def test_an_exception_anywhere_in_the_audio_path_is_contained():
    voice_link = _with_prefs({11: ON})

    def boom(text, prefs):
        raise RuntimeError("edge-tts 503")

    voice_link.render = boom
    os.environ["KASH_BOT_TOKEN"] = "test-token"
    out = run_update.push_telegram_voice("hello", [11], request_post=FakePost())
    assert out[11].startswith("error: "), "the failure is reported, and the run continues"


def test_without_a_bot_token_nothing_is_attempted():
    _with_prefs({11: ON})
    os.environ.pop("KASH_BOT_TOKEN", None)
    post = FakePost()
    assert run_update.push_telegram_voice("hello", [11], request_post=post) == {
        11: "skipped (no KASH_BOT_TOKEN)"}
    assert post.calls == []


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
