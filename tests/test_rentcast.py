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


def _fetch_zip_calls(config, prefs):
    """Run fetch() against a faked API; return the zipCode of each call made."""
    from kash.adapters import rentcast as rc

    calls = []

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(params["zipCode"])
        return R()

    old_get, old_key = rc.requests.get, os.environ.get("RENTCAST_API_KEY")
    rc.requests.get = fake_get
    os.environ["RENTCAST_API_KEY"] = "test-key"
    try:
        RentCastAdapter(config=config).fetch(prefs)
        return calls
    finally:
        rc.requests.get = old_get
        if old_key is None:
            os.environ.pop("RENTCAST_API_KEY", None)
        else:
            os.environ["RENTCAST_API_KEY"] = old_key


def test_config_zips_override_caps_the_quota_spend():
    """Each ZIP is one call against the 50/month quota, so this source keeps its own
    6-ZIP list rather than inheriting the widened 11-ZIP search allow-list."""
    core = ["10304", "10306", "10308", "10309", "10312", "10314"]
    prefs = {"zips": core + ["10301", "10302", "10303", "10305", "10310"]}
    assert _fetch_zip_calls({"zips": core}, prefs) == core


def test_without_the_override_prefs_zips_are_used():
    assert _fetch_zip_calls({}, {"zips": ["10308", "10312"]}) == ["10308", "10312"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
