#!/usr/bin/env python3
"""Calculated columns stay correct through every write path, and protected fields are safe.

update_fields used to issue a bare UPDATE, so enriching a row with its property tax or
estimated rent left monthly_piti stale. On the live pool that meant 34 of 67 rows carried a
wrong PITI, the worst $545/month out — the number a buyer uses to decide affordability.

Its docstring also claimed it never touched user-protected fields, while filtering only on
FIELD_ORDER. Temp DBs, no network.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import finance                       # noqa: E402
from kash.dedup import match_key                # noqa: E402
from kash.schema import CALC_FIELDS             # noqa: E402
from kash.store import Store                    # noqa: E402

FIN = {"rate": 0.063, "down_payment_pct": 0.20, "term_years": 30}


def fresh(**over):
    s = Store(os.path.join(tempfile.mkdtemp(), "t.db"), finance_cfg=FIN)
    row = {"street_address": "1 Test Ave", "zip": "10308", "list_price": 700000,
           "beds": "3", "baths": "2", "property_type": "sf_detached", "sqft": 1500}
    row.update(over)
    s.upsert(row, source="test")
    return s, match_key(row)


def get(s, field):
    return s.all()[0].get(field)


# --- the regression -------------------------------------------------------------------------

def test_enriching_tax_refreshes_monthly_piti():
    """The exact live failure: detail enrichment writes the tax, PITI must follow."""
    s, key = fresh()
    before = get(s, "monthly_piti")
    s.update_fields(key, {"property_tax_annual": 12000})
    after = get(s, "monthly_piti")
    assert after != before, "PITI did not move after the tax was written"
    assert after > before


def test_enriching_rent_refreshes_cap_rate():
    s, key = fresh()
    assert get(s, "cap_rate") is None
    s.update_fields(key, {"estimated_rent_monthly": 4000})
    assert get(s, "cap_rate") is not None


def test_price_per_sqft_follows_a_sqft_correction():
    s, key = fresh()
    s.update_fields(key, {"sqft": 2000})
    assert get(s, "price_per_sqft") == 350


def test_price_drop_pct_follows_original_list_price():
    s, key = fresh()
    assert get(s, "price_drop_pct") in (None, 0)
    s.update_fields(key, {"original_list_price": 800000})
    assert get(s, "price_drop_pct") == 12.5


def test_every_calc_field_matches_a_fresh_recompute():
    s, key = fresh()
    s.update_fields(key, {"property_tax_annual": 11000, "estimated_rent_monthly": 3800,
                          "hoa_monthly": 200, "sqft": 1800})
    row = s.all()[0]
    fresh_calc = finance.recompute(dict(row), FIN)
    for f in CALC_FIELDS:
        assert row.get(f) == fresh_calc.get(f), f"{f}: stored {row.get(f)} vs {fresh_calc.get(f)}"


# --- protected fields -----------------------------------------------------------------------

def test_protected_fields_are_refused_by_default():
    s, key = fresh()
    s.update_fields(key, {"my_notes": "x", "analysis": "y", "tier": "S"})
    row = s.all()[0]
    assert row.get("my_notes") is None and row.get("analysis") is None and row.get("tier") is None


def test_protected_fields_are_written_when_explicitly_allowed():
    s, key = fresh()
    s.update_fields(key, {"tier": "S", "analysis": "auto"}, allow_protected=True)
    row = s.all()[0]
    assert row.get("tier") == "S" and row.get("analysis") == "auto"


def test_unprotected_fields_still_write_alongside_a_refused_one():
    s, key = fresh()
    s.update_fields(key, {"my_notes": "nope", "year_built": 1965})
    row = s.all()[0]
    assert row.get("year_built") == 1965 and row.get("my_notes") is None


# --- coercion and validation (this path bypasses pydantic) ----------------------------------

def test_numeric_strings_are_coerced():
    s, key = fresh()
    s.update_fields(key, {"year_built": "1965"})
    assert get(s, "year_built") == 1965


def test_an_invalid_enum_cannot_overwrite_a_good_value():
    """A bad status would make the row invisible to the alert gate, which filters on
    status == 'active'."""
    s, key = fresh()
    s.update_fields(key, {"status": "active"})
    assert get(s, "status") == "active"
    s.update_fields(key, {"status": "for_sale_ish"})
    assert get(s, "status") == "active", "an invalid enum must not overwrite a valid one"


def test_a_valid_enum_is_written():
    s, key = fresh()
    s.update_fields(key, {"status": "pending"})
    assert get(s, "status") == "pending"


def test_non_numeric_junk_is_dropped():
    s, key = fresh()
    s.update_fields(key, {"year_built": "not a year"})
    assert get(s, "year_built") is None


def test_calc_fields_cannot_be_set_directly():
    """They are derived; letting a caller set one would be silently overwritten anyway."""
    s, key = fresh()
    s.update_fields(key, {"monthly_piti": 99999})
    assert get(s, "monthly_piti") != 99999


def test_none_clears_a_field():
    s, key = fresh(year_built=1970)
    s.update_fields(key, {"year_built": None})
    assert get(s, "year_built") is None


# --- guards ---------------------------------------------------------------------------------

def test_recompute_covers_every_declared_calc_field():
    out = finance.recompute({"list_price": 700000, "sqft": 1000}, FIN)
    assert set(CALC_FIELDS) <= set(out)


def test_updating_a_missing_row_reports_failure():
    s, _ = fresh()
    assert s.update_fields("no:such|key", {"year_built": 1965}) is False


def test_empty_update_is_a_no_op():
    s, key = fresh()
    assert s.update_fields(key, {}) is False
    assert s.update_fields(key, {"not_a_column": 1}) is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
