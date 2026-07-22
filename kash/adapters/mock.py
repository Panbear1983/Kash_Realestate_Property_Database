"""MockAdapter — fixture data for testing the pipeline end-to-end with no credentials.

The fixtures are shaped to exercise every merge path against the seeded pool:
  * 45 Fairlawn Loop  -> price drop on an existing row (tests price_drop + note-preserve)
  * 354 Doane Ave     -> status change to pending (tests status_change)
  * two new addresses -> inserts, carrying the new schema fields (year_built, hoa, rent...)
"""
from __future__ import annotations

from .base import SourceAdapter

_FIXTURES = [
    {  # existing row, dropped from 709,900 -> 699,000
        "street_address": "45 Fairlawn Loop", "zip": "10308",
        "neighborhood": "Great Kills", "property_type": "sf_attached",
        "list_price": 699000, "status": "active", "days_on_market": 34,
        "year_built": 1985, "property_tax_annual": 6200, "hoa_monthly": 0,
        "estimated_rent_monthly": 3200, "photo_count": 22,
        "listing_agent": "Jane Rossi", "listing_brokerage": "Neuhaus Realty",
        "source_url": "https://example.com/45-fairlawn-loop",
    },
    {  # existing row, went pending
        "street_address": "354 Doane Ave", "zip": "10308",
        "neighborhood": "Great Kills", "property_type": "2fam_detached",
        "list_price": 898989, "status": "pending", "days_on_market": 67,
        "source_url": "https://example.com/354-doane-ave",
    },
    {  # brand-new listing
        "street_address": "12 Sprague Ave", "zip": "10307",
        "neighborhood": "Tottenville", "property_type": "sf_detached",
        "is_multifamily": False, "list_price": 689000, "status": "active",
        "beds": "4", "baths": "3", "sqft": 1820, "days_on_market": 3,
        "year_built": 1978, "lot_size_sqft": 4000, "property_tax_annual": 5800,
        "hoa_monthly": 0, "estimated_rent_monthly": 3000, "condition": "move_in",
        "basement": "finished", "garage_spaces": 1, "photo_count": 31,
        "mls_number": "SI-77120", "school_name": "PS 1 Tottenville",
        "listing_description": "Renovated 4BR with finished basement and updated kitchen.",
        "source_url": "https://example.com/12-sprague-ave",
    },
    {  # brand-new listing
        "street_address": "88 Barlow Ave", "zip": "10312",
        "neighborhood": "Eltingville", "property_type": "sf_semi",
        "is_multifamily": False, "list_price": 745000, "status": "active",
        "beds": "3", "baths": "2", "sqft": 1560, "days_on_market": 9,
        "year_built": 1992, "property_tax_annual": 6100, "hoa_monthly": 0,
        "estimated_rent_monthly": 3100, "condition": "tlc", "photo_count": 14,
        "mls_number": "SI-77340",
        "listing_description": "Well-kept semi near PS 42; some updating needed.",
        "source_url": "https://example.com/88-barlow-ave",
    },
]


class MockAdapter(SourceAdapter):
    name = "mock"

    def fetch(self, preferences: dict) -> list[dict]:
        return [self._stamp(dict(r)) for r in _FIXTURES]
