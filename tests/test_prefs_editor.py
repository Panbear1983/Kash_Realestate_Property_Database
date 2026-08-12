#!/usr/bin/env python3
"""Preferences editing: the spec/validate/save layer, plus a headless run of the Textual
screen (textual's Pilot driver — no real terminal). preferences.yaml stays the single
source of truth; these tests guard the two failure modes that matter: a bad value reaching
the file, and an edit destroying parts of the file the screen doesn't manage."""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from kash import preferences, prefs_editor as pe  # noqa: E402

SAMPLE = {
    "market": "Staten Island, NY",
    "zips": ["10304", "10308"],
    "price": {"min": 560915, "max": 800000},
    "property_types": ["sf_detached", "2fam_detached"],
    "beds_min": 3,
    "eligibility": {"store_min_baths": 2, "telegram_min_baths": 2.5,
                    "telegram_max_price": 800000, "safe_flood_zones": ["X"]},
    "telegram_recipient_ids": [111, 222],
    "llm": {"backend": "auto",
            "budgets": {"per_actor": {"111": {"requests_per_day": 200}}}},
    "sources": {"zillow": {"every_days": 1, "results_limit": 40},
                "rentcast": {"every_days": 7}},
    "finance": {"rate": 0.063},
}


def sample():
    import copy
    return copy.deepcopy(SAMPLE)


# --- coercion & validation ------------------------------------------------------------------

def test_numbers_accept_human_formatting():
    f = next(x for x in pe.FIELDS if x.path == "price.max")
    assert pe.coerce(f, "$780,000") == 780000


def test_whole_float_stays_an_int():
    f = next(x for x in pe.FIELDS if x.path == "eligibility.store_min_baths")
    assert pe.coerce(f, "2.0") == 2
    assert pe.coerce(f, "2.5") == 2.5


def test_out_of_range_and_junk_are_rejected_with_the_label():
    f = next(x for x in pe.FIELDS if x.path == "beds_min")
    for bad in ("0", "99", "three", ""):
        try:
            pe.coerce(f, bad)
        except ValueError as e:
            assert "Beds minimum" in str(e)
        else:
            raise AssertionError(f"{bad!r} must be rejected")


def test_empty_multi_select_is_rejected():
    f = next(x for x in pe.FIELDS if x.path == "zips")
    try:
        pe.coerce(f, [])
    except ValueError as e:
        assert "at least one" in str(e)
    else:
        raise AssertionError("an empty allow-list must be rejected")


def test_inverted_price_range_is_caught():
    p = sample()
    p["price"] = {"min": 800000, "max": 700000}
    assert any("below price max" in e for e in pe.cross_validate(p))


def test_alert_cap_below_price_min_is_caught():
    p = sample()
    p["eligibility"]["telegram_max_price"] = 500000
    assert any("could ever alert" in e for e in pe.cross_validate(p))


def test_apply_never_mutates_on_error():
    p = sample()
    new, errors = pe.apply(p, {"price.min": "junk", "beds_min": "4"})
    assert errors and p["beds_min"] == 3
    assert new["price"]["min"] == 560915, "failed batch must leave values unapplied"


def test_apply_rejects_paths_outside_the_spec():
    _, errors = pe.apply(sample(), {"telegram_recipient_ids": "[999]"})
    assert any("Not an editable preference" in e for e in errors)


def test_hand_added_universe_values_are_kept():
    p = sample()
    p["zips"].append("07008")            # hand-added, outside the SI universe
    f = next(x for x in pe.FIELDS if x.path == "zips")
    assert "07008" in pe.universe_for(f, p)


# --- saving ---------------------------------------------------------------------------------

def test_save_backs_up_and_preserves_unmanaged_keys():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "preferences.yaml")
        preferences.save(sample(), path)
        p = preferences.load(path)
        new, errors = pe.apply(p, {"price.max": "780000"})
        assert not errors
        backup = pe.save_with_backup(new, path)
        assert os.path.exists(backup) and "prefs_backups" in backup
        assert yaml.safe_load(open(backup))["price"]["max"] == 800000

        saved = preferences.load(path)
        assert saved["price"]["max"] == 780000
        # Everything the screen does not manage survives the round-trip untouched.
        assert saved["llm"]["budgets"]["per_actor"] == {"111": {"requests_per_day": 200}}
        assert saved["finance"]["rate"] == 0.063
        assert saved["telegram_recipient_ids"] == [111, 222]
        assert not os.path.exists(path + ".tmp")


# --- the Textual screen, headless -----------------------------------------------------------

def test_screen_edits_toggle_and_save_end_to_end():
    from textual.app import App
    from textual.widgets import Checkbox, Input, Static

    from kash.prefs_screen import PreferencesScreen, _oid, _wid

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "preferences.yaml")
        preferences.save(sample(), path)

        class Harness(App):
            pass

        async def drive():
            app = Harness()
            async with app.run_test(size=(100, 50)) as pilot:
                screen = PreferencesScreen(path)
                await app.push_screen(screen)
                await pilot.pause()

                # every editable field rendered a widget
                for f in pe.FIELDS:
                    if f.kind == "multi":
                        assert screen.query(Checkbox).filter(f"#{_oid(f.path, f.universe[0])}")
                    else:
                        assert screen.query_one(f"#{_wid(f.path)}")

                # a bad edit is blocked with the message on screen, nothing saved
                box = screen.query_one(f"#{_wid('price.max')}", Input)
                box.value = "not-a-price"
                screen.query_one("#pref-save").press()
                await pilot.pause()
                assert "not a number" in str(screen.query_one("#pref-errors", Static).render())
                assert preferences.load(path)["price"]["max"] == 800000

                # fix it, toggle a ZIP on, save — dismisses True and writes the file
                box.value = "  $785,000 "
                screen.query_one(f"#{_oid('zips', '10312')}", Checkbox).value = True
                screen.query_one("#pref-save").press()
                await pilot.pause()

            saved = preferences.load(path)
            assert saved["price"]["max"] == 785000
            assert "10312" in saved["zips"] and "10304" in saved["zips"]

        asyncio.run(drive())


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
