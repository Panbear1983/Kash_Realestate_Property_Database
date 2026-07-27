#!/usr/bin/env python3
"""Tracking-sheet importer: cell parsers on real captured text, and the merge policy.

Every parser fixture below is a string actually observed in the PDF's extracted text —
including the glued-column cases that produced the three bugs found during dry runs
(sequence stall on a mangled row number, "$718KEst." killing the word boundary, and
"Est." + space never matching a trailing \\b). No network, temp DBs only.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.import_tracking_sheet import (  # noqa: E402
    _money_k, merge_existing, parse_bd_ba, parse_price_cell, parse_school, parse_zest,
    split_rows)
from kash.dedup import match_key  # noqa: E402
from kash.store import Store      # noqa: E402


# --- school column --------------------------------------------------------------------------

def test_est_prefix_means_unverified():
    name, rating, verified = parse_school("Est. PS 8/PS 32 GS 8/10")
    assert (name, rating, verified) == ("PS 8/PS 32", 8, False)


def test_verify_prefix_means_unverified():
    name, rating, verified = parse_school("Huguenot — VERIFY PS 5/PS 6 GS 7/10")
    assert (name, rating, verified) == ("PS 5/PS 6", 7, False)


def test_bare_school_is_verified():
    name, rating, verified = parse_school("PS 29 Bardwell GS 7/10")
    assert (name, rating, verified) == ("PS 29 Bardwell", 7, True)


def test_verify_alone_yields_nothing():
    assert parse_school("Verify") == (None, None, None)


# --- zestimate column (returns chars consumed so "Est." never glues on) ---------------------

def test_zest_plain():
    assert parse_zest("$718KEst. PS 8...")[:2] == (718000, None)


def test_zest_consumption_prevents_the_glue_bug():
    """The bug: '$718KEst.' left in place made 'Est.' unmatchable."""
    txt = "$718KEst. PS 8/PS 32 GS 8/10"
    z, r, consumed = parse_zest(txt)
    assert parse_school(txt[consumed:])[2] is False, "school must read as unverified"


def test_zest_redfin_goes_to_redfin_estimate():
    z, r, _ = parse_zest("$907K (Redfin)...")
    assert (z, r) == (None, 907000)


def test_no_zest_is_none_but_still_consumed():
    z, r, consumed = parse_zest("No ZestEst. PS 23 GS 8/10")
    assert (z, r) == (None, None) and consumed >= len("No Zest")


def test_zest_with_arrow():
    assert parse_zest("$916K↑PS 36...")[:2] == (916000, None)


# --- beds/baths and price cells -------------------------------------------------------------

def test_bd_ba_with_bath_flag():
    beds, baths, note = parse_bd_ba("3/2 +⚠️$30K")
    assert (beds, baths) == ("3", "2") and "$30K" in note


def test_bd_ba_partial_na():
    assert parse_bd_ba("N/A/3")[:2] == (None, "3")
    assert parse_bd_ba("3/N/A")[:2] == ("3", None)


def test_bd_ba_fully_missing():
    assert parse_bd_ba("N/A1,800")[:2] == (None, None)


def test_price_cell_with_relist_original():
    price, orig, note = parse_price_cell("$709,900 Relisted, was $719,800")
    assert (price, orig) == (709900, 719800) and "Relisted" in note


def test_price_cell_with_cut_from():
    price, orig, _ = parse_price_cell("$744,900 ↓ from $758,888")
    assert (price, orig) == (744900, 758888)


def test_price_cell_plain():
    price, orig, note = parse_price_cell("$754,999 Active 30d")
    assert (price, orig, note) == (754999, None, "Active 30d")


def test_bid_range_takes_the_midpoint():
    assert _money_k("~$630-650K Street comps")[0] == 640000


# --- row sequencing -------------------------------------------------------------------------

def test_a_mangled_row_number_does_not_stall_the_sequence():
    """The bug that cost 55 rows: requiring exactly-next numbers stalls at one casualty."""
    txt = ("1NOWAHuguenot109 Cardiff St, 10312$754,999 x"
           "2NOWSGreat Kills45 Fairlawn Loop, 10308$709,900 x"
           "0RTH mangled row three garbage "                       # row 3's anchor destroyed
           "4NOWBGreat Kills37 Middle Loop Rd, 10308$738,999 x")
    rows, missing = split_rows(txt)
    assert [n for n, _, _ in rows] == [1, 2, 4]
    assert missing[:1] == [3]


# --- merge policy on a real Store -----------------------------------------------------------

def _store_with(row):
    s = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    s.upsert(row, source="test")
    return s, match_key(row)


BASE = {"street_address": "1 Test Ave", "zip": "10308", "list_price": 700000,
        "beds": "3", "baths": "2", "property_type": "sf_detached"}


def test_machine_fields_never_overwrite():
    s, key = _store_with(dict(BASE, sqft=1500))
    rec = dict(BASE, sqft=9999, zestimate=750000, street_address="1 Test Ave")
    merge_existing(s, key, s.get(key), rec, apply=True, diffs=[])
    row = s.get(key)
    assert row["sqft"] == 1500, "the scraper's value must win"
    assert row["zestimate"] == 750000, "but a NULL must be filled"


def test_curated_fields_overwrite_and_are_diffed():
    s, key = _store_with(BASE)
    s.update_fields(key, {"tier": "B", "analysis": "[auto] machine guess"},
                    allow_protected=True)
    diffs = []
    rec = dict(BASE, tier="S", analysis="Human curation from the sheet.")
    merge_existing(s, key, s.get(key), rec, apply=True, diffs=diffs)
    row = s.get(key)
    assert row["tier"] == "S" and row["analysis"] == "Human curation from the sheet."
    assert {d[1] for d in diffs} == {"tier", "analysis"}


def test_an_empty_sheet_cell_keeps_the_pool_value():
    s, key = _store_with(BASE)
    s.update_fields(key, {"my_notes": "precious"}, allow_protected=True)
    merge_existing(s, key, s.get(key), dict(BASE, my_notes=None), apply=True, diffs=[])
    assert s.get(key)["my_notes"] == "precious"


def test_a_truncated_rebuild_never_replaces_a_longer_note():
    """52 Gadsen Pl: the sheet holds 'I love the layout.' — a prefix of the pool's fuller
    note. Overwriting would lose words while adding none."""
    s, key = _store_with(BASE)
    s.update_fields(key, {"my_notes": "I love the layout. If only this was in range."},
                    allow_protected=True)
    diffs = []
    merge_existing(s, key, s.get(key), dict(BASE, my_notes="I love the layout."),
                   apply=True, diffs=diffs)
    assert s.get(key)["my_notes"] == "I love the layout. If only this was in range."
    assert not diffs


def test_a_genuinely_different_note_does_replace():
    s, key = _store_with(BASE)
    s.update_fields(key, {"my_notes": "old thought"}, allow_protected=True)
    merge_existing(s, key, s.get(key), dict(BASE, my_notes="new observation entirely"),
                   apply=True, diffs=[])
    assert s.get(key)["my_notes"] == "new observation entirely"


def test_dry_run_writes_nothing():
    s, key = _store_with(BASE)
    before = dict(s.get(key))
    rec = dict(BASE, tier="S", zestimate=750000, my_notes="sheet note")
    diffs = []
    merge_existing(s, key, s.get(key), rec, apply=False, diffs=diffs)
    assert dict(s.get(key)) == before, "apply=False must not touch the row"
    assert diffs, "but the diff must still be reported"


def test_merge_is_idempotent():
    s, key = _store_with(BASE)
    rec = dict(BASE, tier="S", analysis="Sheet analysis.", zestimate=750000)
    merge_existing(s, key, s.get(key), rec, apply=True, diffs=[])
    second = []
    changed = merge_existing(s, key, s.get(key), rec, apply=True, diffs=second)
    assert not changed and not second, "a re-run must be a no-op"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
