#!/usr/bin/env python3
"""RentCast adapter normalization tests; no provider request is made."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.adapters.rentcast import RentCastAdapter  # noqa: E402


def test_rentcast_listing_url_is_preserved_for_dashboard_and_telegram_links():
    raw = {
        "addressLine1": "12 Example Street",
        "zipCode": "10308",
        "price": 700000,
        "propertyType": "Single Family",
        "bedrooms": 4,
        "bathrooms": 2.5,
        "listingUrl": "https://example.com/listing/12-example",
    }
    record = RentCastAdapter()._normalize(raw)
    assert record["source_url"] == raw["listingUrl"]
    assert record["listing_url"] == raw["listingUrl"]


if __name__ == "__main__":
    test_rentcast_listing_url_is_preserved_for_dashboard_and_telegram_links()
    print("PASS — RentCast listing URL is available to the UI and notifications")
