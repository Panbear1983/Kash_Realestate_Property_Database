#!/usr/bin/env python3
"""Completeness spec and audit. Temp DB, no network."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import completeness as comp  # noqa: E402
from kash.ledger import Ledger          # noqa: E402
from kash.store import Store            # noqa: E402


def row(**over):
    """A scraped-looking row. Deliberately omits street_address so store_with() can assign a
    unique one — rows sharing an address collapse into a single row via match_key."""
    base = {"zip": "10308", "list_price": 700000, "beds": "3", "baths": "2",
            "property_type": "sf_detached", "listing_url": "https://example.com/1"}
    base.update(over)
    return base


def store_with(*rows):
    s = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    for i, r in enumerate(rows):
        r.setdefault("street_address", f"{i} Test Ave")
        s.upsert(r, source="test")
    return s


# --- the spec -------------------------------------------------------------------------------

def test_human_owned_fields_are_never_counted_as_missing():
    """Otherwise the report drowns in blanks the buyer simply hasn't filled in."""
    for f in ("my_notes", "favorite", "viewing_status", "property_id", "rank"):
        assert comp.FIELD_SPEC[f]["filler"] is None
        assert f not in comp.MACHINE_FIELDS


def test_every_machine_field_names_a_filler():
    for f in comp.MACHINE_FIELDS:
        assert comp.FIELD_SPEC[f]["filler"], f


def test_flood_zone_blocks_alerts():
    assert "flood_zone" in comp.ALERT_FIELDS


def test_missing_fields_respects_alert_only():
    r = row(flood_zone=None, year_built=None)
    assert "flood_zone" in comp.missing_fields(r, alert_only=True)
    assert "year_built" not in comp.missing_fields(r, alert_only=True)
    assert "year_built" in comp.missing_fields(r)


def test_empty_string_counts_as_missing():
    assert comp.is_missing({"flood_zone": ""}, "flood_zone") is True
    assert comp.is_missing({"flood_zone": "X"}, "flood_zone") is False


# --- the audit ------------------------------------------------------------------------------

def test_audit_separates_curated_from_scraped():
    s = store_with(row(property_id="P-1", flood_zone="X"), row(flood_zone=None))
    a = comp.audit(s)
    assert a["curated"] == 1 and a["scraped"] == 1
    assert a["fields"]["flood_zone"]["scraped_missing"] == 1
    assert a["fields"]["flood_zone"]["curated_missing"] == 0


def test_audit_counts_alert_blocked_rows_and_why():
    s = store_with(row(flood_zone=None), row(flood_zone="X"))
    a = comp.audit(s)
    assert a["alert_blocked"] == 1
    assert a["alert_blocked_reasons"]["flood_zone"] == 1


def test_audit_groups_gaps_by_filler():
    s = store_with(row(flood_zone=None, neighborhood=None))
    a = comp.audit(s)
    assert a["by_filler"]["enrich.flood"] >= 1
    assert a["by_filler"]["geo_static"] >= 1


def test_coverage_is_one_when_nothing_is_missing():
    s = store_with(row(property_id="P-1"))     # curated only; no scraped rows
    a = comp.audit(s)
    assert a["fields"]["flood_zone"]["coverage"] == 1.0


def test_audit_includes_ledger_summary_when_given_one():
    s = store_with(row(flood_zone=None))
    lg = Ledger(s)
    lg.record_failure("addr:0 test ave|10308", "flood_zone", "SSLEOFError")
    a = comp.audit(s, lg)
    assert any(e["field"] == "flood_zone" and e["outstanding"] == 1 for e in a["ledger"])


def test_audit_omits_ledger_when_not_given_one():
    assert "ledger" not in comp.audit(store_with(row()))


# --- the report -----------------------------------------------------------------------------

def test_report_names_alert_blocking_gaps():
    s = store_with(row(flood_zone=None))
    text = comp.format_report(comp.audit(s))
    assert "blocked from alerting" in text
    assert "flood_zone" in text


def test_verbose_report_lists_fields_and_their_filler():
    s = store_with(row(flood_zone=None))
    text = comp.format_report(comp.audit(s), verbose=True)
    assert "enrich.flood" in text
    assert "blocks alerts" in text


def test_report_surfaces_the_last_ledger_error():
    s = store_with(row(flood_zone=None))
    lg = Ledger(s)
    lg.record_failure("addr:0 test ave|10308", "flood_zone", "SSLEOFError: handshake")
    text = comp.format_report(comp.audit(s, lg))
    assert "SSLEOFError" in text


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
