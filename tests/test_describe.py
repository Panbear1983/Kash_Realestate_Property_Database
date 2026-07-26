#!/usr/bin/env python3
"""LLM description-signal extraction: selection, persistence, idempotence, and the
keyword fallback it augments. Fake backend throughout — no network, temp DB only.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.enrich.describe import _needs_extract, extract_descriptions  # noqa: E402
from kash.signals import multigenerational                             # noqa: E402
from kash.store import Store                                           # noqa: E402

# The case the keyword matcher misses entirely: a mother/daughter setup described without
# any of the literal terms in kash/signals.py's _MULTIGENERATIONAL tuple.
PARAPHRASE = ("Charming colonial with a private side entry leading to a finished lower level "
              "with a kitchenette and full bath. Bright kitchen, deep yard.")


class FakeBackend:
    def __init__(self, spec=None, ok=True, raises=None):
        self._spec = spec or {
            "multigenerational": True, "separate_entrance": True, "second_kitchen": True,
            "condition": "turnkey", "friction": [], "evidence": "private side entry",
        }
        self._ok, self._raises = ok, raises
        self.calls = 0

    def available(self):
        return self._ok, "" if self._ok else "simulated outage"

    def query_spec(self, prompt, schema):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._spec


def make_store(rows):
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    store = Store(db)
    for r in rows:
        store.upsert(r, source="test")
    return store


def base_row(**over):
    row = {"street_address": "1 Test Ave", "zip": "10308", "list_price": 700000,
           "beds": "3", "baths": "2", "property_type": "sf_detached",
           "listing_description": PARAPHRASE}
    row.update(over)
    return row


# --- selection ------------------------------------------------------------------------------

def test_row_with_description_and_no_extraction_is_selected():
    assert _needs_extract({"listing_description": "x"}) is True


def test_row_without_a_description_is_skipped():
    assert _needs_extract({"listing_description": None}) is False
    assert _needs_extract({}) is False


def test_already_extracted_row_is_skipped():
    assert _needs_extract({"listing_description": "x", "signal_extracted_at": "2026-07-26"}) is False


# --- persistence ----------------------------------------------------------------------------

def test_extraction_writes_signal_columns():
    store = make_store([base_row()])
    fake = FakeBackend()
    out = extract_descriptions(store, {}, backend=fake)
    assert out["extracted"] == 1, out
    row = store.all()[0]
    assert row["signal_multigenerational"] is True
    assert row["signal_separate_entrance"] is True
    assert row["signal_condition"] == "turnkey"
    assert row["signal_evidence"] == "private side entry"
    assert row["signal_extracted_at"]


def test_friction_list_round_trips_as_json():
    store = make_store([base_row()])
    fake = FakeBackend(spec={"multigenerational": False, "separate_entrance": False,
                             "second_kitchen": False, "condition": "gut",
                             "friction": ["busy road", "as-is sale"], "evidence": "as is"})
    extract_descriptions(store, {}, backend=fake)
    assert store.all()[0]["signal_friction"] == ["busy road", "as-is sale"]


def test_comma_string_friction_is_tolerated():
    """A backend without a schema flag may return a string where a list was asked for."""
    store = make_store([base_row()])
    fake = FakeBackend(spec={"multigenerational": False, "separate_entrance": False,
                             "second_kitchen": False, "condition": "cosmetic",
                             "friction": "busy road, no parking", "evidence": ""})
    extract_descriptions(store, {}, backend=fake)
    assert store.all()[0]["signal_friction"] == ["busy road", "no parking"]


# --- idempotence and failure ----------------------------------------------------------------

def test_second_run_makes_no_calls():
    store = make_store([base_row()])
    fake = FakeBackend()
    extract_descriptions(store, {}, backend=fake)
    assert fake.calls == 1
    out = extract_descriptions(store, {}, backend=fake)
    assert fake.calls == 1, "already-extracted rows must not be re-sent"
    assert out["extracted"] == 0


def test_rows_without_descriptions_are_never_sent():
    store = make_store([base_row(listing_description=None)])
    fake = FakeBackend()
    out = extract_descriptions(store, {}, backend=fake)
    assert fake.calls == 0
    assert out["extracted"] == 0


def test_unavailable_backend_reports_and_writes_nothing():
    store = make_store([base_row()])
    out = extract_descriptions(store, {}, backend=FakeBackend(ok=False))
    assert out["extracted"] == 0
    assert "skipped" in out
    assert store.all()[0]["signal_extracted_at"] is None


def test_one_failing_row_does_not_end_the_batch():
    store = make_store([base_row()])
    out = extract_descriptions(store, {}, backend=FakeBackend(raises=RuntimeError("boom")))
    assert out["extracted"] == 0
    assert out.get("failed") == 1


def test_limit_bounds_the_batch():
    store = make_store([base_row(street_address=f"{i} Test Ave") for i in range(5)])
    fake = FakeBackend()
    out = extract_descriptions(store, {}, limit=2, backend=fake)
    assert fake.calls == 2 and out["extracted"] == 2


# --- what this is all for -------------------------------------------------------------------

def test_paraphrase_is_missed_by_keywords_but_caught_by_extraction():
    """The whole point: no literal term appears, so only the semantic read finds it."""
    keyword_only = {"listing_description": PARAPHRASE}
    assert multigenerational(keyword_only) == (False, "")

    extracted = {**keyword_only, "signal_extracted_at": "2026-07-26",
                 "signal_multigenerational": True, "signal_separate_entrance": True,
                 "signal_evidence": "private side entry"}
    flagged, why = multigenerational(extracted)
    assert flagged is True and why == "private side entry"


def test_keyword_path_still_works_when_not_yet_extracted():
    assert multigenerational({"listing_description": "Home with a separate entrance."}) == (
        True, "separate entrance")


def test_union_means_extraction_never_drops_a_keyword_hit():
    """Extraction saying 'no' must not lose a listing the old path would have flagged."""
    row = {"listing_description": "Home with a separate entrance.",
           "signal_extracted_at": "2026-07-26", "signal_multigenerational": False,
           "signal_separate_entrance": False}
    assert multigenerational(row)[0] is True


def test_no_signal_and_no_keyword_is_not_flagged():
    assert multigenerational({"listing_description": "Bright kitchen, deep yard."}) == (False, "")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
