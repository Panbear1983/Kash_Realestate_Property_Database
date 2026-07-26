#!/usr/bin/env python3
"""Zillow detail normalization tests; no Apify request is made."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.enrich.detail import _needs_detail, _normalize  # noqa: E402


def test_detail_description_adds_machine_priority_note_without_user_fields():
    record = _normalize({
        "homeType": "SINGLE_FAMILY",
        "description": "Lower-level studio with separate entrance and second kitchen.",
        "resoFacts": {},
    })
    assert "separate entrance" in record["priority_note"]
    assert "second kitchen" in record["priority_note"]
    assert "analysis" not in record
    assert "my_notes" not in record


def test_detail_queue_requires_real_zillow_https_host():
    assert _needs_detail({"listing_url": "https://www.zillow.com/homedetails/example/123_zpid/", "year_built": None}) is True
    assert _needs_detail({"listing_url": "https://evil.example/?next=zillow.com/123_zpid", "year_built": None}) is False
    assert _needs_detail({"listing_url": "http://www.zillow.com/homedetails/example/123_zpid/", "year_built": None}) is False


if __name__ == "__main__":
    test_detail_description_adds_machine_priority_note_without_user_fields()
    test_detail_queue_requires_real_zillow_https_host()
    print("PASS — detail descriptions populate only machine review notes")
