#!/usr/bin/env python3
"""The buyer's own fields: validation, writing, history, and who else may touch them.

These columns are USER_PROTECTED — no provider merge may write them — so this is the one
sanctioned write path, and the tests pin both halves: that the owner CAN record a tour, and
that a bad value never lands silently (the store's coercion used to drop invalid enums, so a
typo looked accepted and changed nothing). Also covers the headless screen. No network.
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import crm, lifecycle  # noqa: E402
from kash.dedup import match_key  # noqa: E402
from kash.store import Store  # noqa: E402


def row(addr="45 Fairlawn Loop"):
    return {"street_address": addr, "zip": "10308", "list_price": 700000, "beds": "3",
            "baths": "2", "property_type": "sf_detached", "status": "active"}


def fresh(addr="45 Fairlawn Loop"):
    s = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    r = row(addr)
    s.upsert(r, source="zillow")
    return s, match_key(r)


# --- validation -----------------------------------------------------------------------------

def test_a_typo_is_refused_not_silently_dropped():
    s, key = fresh()
    for bad in ({"viewing_status": "scheduledd"}, {"offer_status": "maybe"},
                {"viewing_date": "20 Aug 2026"}, {"user_rating": "9"},
                {"user_rating": "great"}, {"contacted_agent": "sorta"}):
        try:
            crm.apply(s, key, bad)
        except crm.CrmError:
            pass
        else:
            raise AssertionError(f"{bad} must be refused")
    assert s.get(key)["viewing_status"] is None, "nothing may be written by a failed call"


def test_nothing_is_written_when_one_field_in_the_batch_is_bad():
    s, key = fresh()
    try:
        crm.apply(s, key, {"favorite": "yes", "user_rating": "11"})
    except crm.CrmError:
        pass
    assert s.get(key)["favorite"] is None, "a half-applied record is worse than none"


def test_yes_no_spellings_all_work():
    for raw, expected in (("yes", True), ("Y", True), ("true", True), ("1", True),
                          ("no", False), ("N", False), ("false", False), ("0", False)):
        s, key = fresh()
        crm.apply(s, key, {"favorite": raw})
        assert s.get(key)["favorite"] is expected, raw


def test_blank_clears_a_field():
    s, key = fresh()
    crm.apply(s, key, {"user_rating": "4", "my_notes": "great block"})
    crm.apply(s, key, {"user_rating": "", "my_notes": ""})
    stored = s.get(key)
    assert stored["user_rating"] is None and stored["my_notes"] is None


def test_none_is_stored_as_empty_not_as_the_word_none():
    s, key = fresh()
    crm.apply(s, key, {"viewing_status": "scheduled"})
    crm.apply(s, key, {"viewing_status": "none"})
    assert s.get(key)["viewing_status"] is None


def test_an_over_long_note_is_refused_with_its_length():
    s, key = fresh()
    try:
        crm.apply(s, key, {"my_notes": "x" * (crm.MAX_NOTE + 1)})
    except crm.CrmError as e:
        assert str(crm.MAX_NOTE + 1) in str(e)
    else:
        raise AssertionError("an oversized note must be refused")


def test_unknown_fields_are_refused():
    s, key = fresh()
    for bad in ("tier", "list_price", "analysis"):
        try:
            crm.apply(s, key, {bad: "x"})
        except crm.CrmError:
            pass
        else:
            raise AssertionError(f"{bad} is not a CRM field")


# --- writing --------------------------------------------------------------------------------

def test_recording_a_tour_writes_a_protected_field():
    s, key = fresh()
    out = crm.apply(s, key, {"viewing_date": "2026-08-20"})
    stored = s.get(key)
    assert stored["viewing_date"] == "2026-08-20"
    assert stored["viewing_status"] == "scheduled", "a date implies a scheduled viewing"
    assert set(out["changed"]) == {"viewing_date", "viewing_status"}


def test_an_explicit_status_is_not_overridden_by_the_inference():
    s, key = fresh()
    crm.apply(s, key, {"viewing_date": "2026-08-20", "viewing_status": "seen"})
    assert s.get(key)["viewing_status"] == "seen"


def test_a_no_op_save_changes_nothing_and_says_so():
    s, key = fresh()
    crm.apply(s, key, {"favorite": "yes"})
    again = crm.apply(s, key, {"favorite": "yes"})
    assert again == {"changed": {}, "notes": []}


def test_the_change_lands_in_history_readably():
    s, key = fresh()
    crm.apply(s, key, {"offer_status": "offered", "user_rating": "5"})
    events = [e for e in s.recent_changes(10) if e["event"] == "crm_update"]
    assert events, "the buyer's decisions belong in the pool's history"
    detail = events[0]["detail"]
    assert "45 Fairlawn Loop" in detail and "offered" in detail
    assert "unset -> offered" in detail, "an empty field reads as unset, not cleared"
    crm.apply(s, key, {"offer_status": "none"})
    latest = [e for e in s.recent_changes(10) if e["event"] == "crm_update"][0]
    assert "offered -> cleared" in latest["detail"]


def test_a_provider_merge_still_cannot_touch_these_fields():
    s, key = fresh()
    crm.apply(s, key, {"favorite": "yes", "my_notes": "mine", "user_rating": "5"})
    s.upsert({**row(), "favorite": False, "my_notes": "scraped", "user_rating": 1},
             source="zillow")
    stored = s.get(key)
    assert stored["favorite"] is True and stored["my_notes"] == "mine"
    assert stored["user_rating"] == 5


# --- interaction with ageing ------------------------------------------------------------------

def test_a_toured_house_is_protected_from_being_aged_out():
    """Telling Kash you are touring a place also stops a quiet search burying it."""
    s, key = fresh()
    cov = {"source": "zillow", "price_min": 560915, "price_max": 800000, "beds_min": 3,
           "baths_min": 2, "excluded_types": ["condo"], "zips": ["10308"],
           "results_limit": 40, "truncated": False}
    assert lifecycle.is_ageable(s.get(key), cov) is True      # before: fair game
    crm.apply(s, key, {"viewing_date": "2026-08-20"})
    assert lifecycle.is_ageable(s.get(key), cov) is False     # after: hands off


# --- reading --------------------------------------------------------------------------------

def test_summary_reads_like_a_sentence():
    s, key = fresh()
    assert crm.summary(s.get(key)) == "nothing recorded yet"
    crm.apply(s, key, {"favorite": "yes", "viewing_date": "2026-08-20",
                       "offer_status": "considering", "user_rating": "4"})
    text = crm.summary(s.get(key))
    for part in ("favorite", "viewing scheduled 2026-08-20", "offer considering", "rated 4/5"):
        assert part in text, text


def test_upcoming_viewings_are_sorted_and_exclude_the_past():
    s, _ = fresh("1 Alpha St")
    for addr, when in (("2 Beta St", "2026-09-01"), ("3 Gamma St", "2026-08-15"),
                       ("4 Delta St", "2026-07-01")):
        r = row(addr)
        s.upsert(r, source="zillow")
        crm.apply(s, match_key(r), {"viewing_date": when})
    upcoming = crm.upcoming_viewings(s, today="2026-08-12")
    assert [r["street_address"] for r in upcoming] == ["3 Gamma St", "2 Beta St"]


# --- the screen, headless ---------------------------------------------------------------------

def test_screen_saves_and_reports_a_bad_value():
    from textual.app import App
    from textual.widgets import Input, Select, Static

    from kash.crm_screen import CrmScreen, _wid

    s, key = fresh()

    class Harness(App):
        pass

    async def drive():
        app = Harness()
        async with app.run_test(size=(100, 50)) as pilot:
            screen = CrmScreen(s, key)
            await app.push_screen(screen)
            await pilot.pause()

            screen.query_one(f"#{_wid('user_rating')}", Input).value = "11"
            screen.query_one("#crm-save").press()
            await pilot.pause()
            assert "between 1 and 5" in str(screen.query_one("#crm-errors", Static).render())
            assert s.get(key)["user_rating"] is None

            screen.query_one(f"#{_wid('user_rating')}", Input).value = "4"
            screen.query_one(f"#{_wid('viewing_date')}", Input).value = "2026-08-20"
            screen.query_one(f"#{_wid('favorite')}", Select).value = "yes"
            screen.query_one("#crm-save").press()
            await pilot.pause()

        stored = s.get(key)
        assert stored["user_rating"] == 4 and stored["favorite"] is True
        assert stored["viewing_status"] == "scheduled"

    asyncio.run(drive())


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
