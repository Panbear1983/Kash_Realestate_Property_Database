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
    """Each ZIP is one call against the 50/month quota, so zips mode keeps its own
    6-ZIP list rather than inheriting the widened 11-ZIP search allow-list."""
    core = ["10304", "10306", "10308", "10309", "10312", "10314"]
    prefs = {"zips": core + ["10301", "10302", "10303", "10305", "10310"]}
    assert _fetch_zip_calls({"mode": "zips", "zips": core}, prefs) == core


def test_without_the_override_prefs_zips_are_used():
    assert _fetch_zip_calls({"mode": "zips"}, {"zips": ["10308", "10312"]}) == ["10308", "10312"]


# --- the city census ------------------------------------------------------------------------
# One API call for the whole filtered market (RentCast bills per REQUEST): the re-sighting
# engine. The failure modes that matter: paging burning quota on a broken query, a partial
# fetch claiming completeness (false ageing), and an empty census claiming completeness.

CENSUS_PREFS = {"price": {"min": 560915, "max": 900000}, "beds_min": 3,
                "zips": ["10304", "10306", "10308"]}


def _record(i):
    return {"addressLine1": f"{i} Census St", "zipCode": "10304", "price": 700000 + i,
            "propertyType": "Single Family", "bedrooms": 3, "bathrooms": 2.5}


def _census_fetch(pages, total_count=None, config=None, prefs=None, page_raises=None):
    """Drive _fetch_city against scripted pages. `pages` is a list of record-counts;
    `page_raises` (1-based) makes that call raise. Returns (adapter, rows, calls)."""
    from kash.adapters import rentcast as rc

    calls = []
    served = [0]

    class R:
        def __init__(self, n):
            self.headers = ({"X-Total-Count": str(total_count)}
                            if total_count is not None else {})
            self._n = n

        def raise_for_status(self):
            pass

        def json(self):
            start = served[0]
            served[0] += self._n
            return [_record(start + j) for j in range(self._n)]

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(dict(params))
        if page_raises is not None and len(calls) == page_raises:
            raise rc.requests.ConnectionError("census page died")
        return R(pages[len(calls) - 1])

    old_get, old_key = rc.requests.get, os.environ.get("RENTCAST_API_KEY")
    rc.requests.get = fake_get
    os.environ["RENTCAST_API_KEY"] = "test-key"
    try:
        ad = RentCastAdapter(config=config or {})
        rows = ad.fetch(prefs or dict(CENSUS_PREFS))
        return ad, rows, calls
    finally:
        rc.requests.get = old_get
        if old_key is None:
            os.environ.pop("RENTCAST_API_KEY", None)
        else:
            os.environ["RENTCAST_API_KEY"] = old_key


def test_city_mode_is_the_default_and_sends_server_side_filters():
    _, _, calls = _census_fetch(pages=[3], config={"results_limit": 5})
    assert len(calls) == 1
    p = calls[0]
    assert p["city"] == "Staten Island" and p["state"] == "NY"
    assert p["status"] == "Active"
    assert p["price"] == "560915:900000", "the buyer's band, filtered at the server"
    assert p["bedrooms"] == "3:*"
    assert p["limit"] == 5 and p["includeTotalCount"] == "true"
    assert p["offset"] == 0
    assert "zipCode" not in p, "city mode must not also constrain by ZIP"
    assert "bathrooms" not in p, "bath filters would drop null-bath rows policy wants counted"


def test_a_short_page_is_one_call_and_conclusive():
    ad, rows, calls = _census_fetch(pages=[3], config={"results_limit": 5})
    assert len(rows) == 3 and len(calls) == 1
    cov = ad.coverage(len(rows))
    assert cov["truncated"] is False
    assert (cov["price_min"], cov["price_max"]) == (560915, 900000)
    assert cov["beds_min"] == 3
    assert cov["excluded_types"] == [] and cov["zips"] == [], \
        "no type filter and city-wide reach mean no constraint may be claimed"
    assert cov.get("baths_min") is None, "unfiltered fields must not be claimed"


def test_paging_continues_with_offset_until_a_short_page():
    ad, rows, calls = _census_fetch(pages=[5, 2], config={"results_limit": 5})
    assert len(rows) == 7 and len(calls) == 2
    assert [c["offset"] for c in calls] == [0, 5]
    assert ad.coverage(len(rows))["truncated"] is False


def test_the_total_count_header_saves_the_confirming_call():
    ad, rows, calls = _census_fetch(pages=[5], total_count=5, config={"results_limit": 5})
    assert len(calls) == 1, "fetched == totalCount: the empty second call is skipped"
    assert ad.coverage(len(rows))["truncated"] is False


def test_without_a_count_header_paging_stops_at_the_cap_as_truncated():
    """Needing a third page means the filter is being ignored — stop burning quota."""
    ad, rows, calls = _census_fetch(pages=[5, 5, 5], config={"results_limit": 5})
    assert len(calls) == 2 and len(rows) == 10
    assert ad.coverage(len(rows))["truncated"] is True


def test_a_mid_pagination_error_keeps_the_partial_page_and_claims_nothing():
    ad, rows, calls = _census_fetch(pages=[5, 5], config={"results_limit": 5},
                                    page_raises=2)
    assert len(rows) == 5, "the salvaged page must still merge"
    cov = ad.coverage(len(rows))
    assert cov["truncated"] is True and "died" in cov["error"]


def test_a_first_page_error_still_fails_the_source():
    """Schedule backoff (the 403-spiral guard) depends on the source erroring."""
    try:
        _census_fetch(pages=[5], config={"results_limit": 5}, page_raises=1)
    except Exception as e:
        assert "died" in str(e)
    else:
        raise AssertionError("a dead first page must raise")


def test_an_empty_census_is_never_conclusive():
    """Zero rows means a broken query (city typo, filter regression) — claiming
    completeness would start ageing rows on the strength of a bug."""
    ad, rows, calls = _census_fetch(pages=[0], config={"results_limit": 5})
    assert rows == []
    assert ad.coverage(0)["truncated"] is True


def test_city_page_size_defaults_to_the_api_max_not_fetch_limit():
    _, _, calls = _census_fetch(pages=[0], prefs={**CENSUS_PREFS, "fetch_limit": 100})
    assert calls[0]["limit"] == 500, \
        "fetch_limit(100) would quietly turn the one-call census into a paging loop"


def test_zips_mode_reports_no_coverage():
    from kash.adapters import rentcast as rc

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return [_record(1)]

    old_get, old_key = rc.requests.get, os.environ.get("RENTCAST_API_KEY")
    rc.requests.get = lambda *a, **k: R()
    os.environ["RENTCAST_API_KEY"] = "test-key"
    try:
        ad = RentCastAdapter(config={"mode": "zips", "zips": ["10308"]})
        rows = ad.fetch({"zips": ["10308"]})
        assert len(rows) == 1
        assert ad.coverage(len(rows)) is None, "a capped per-ZIP window never proves absence"
    finally:
        rc.requests.get = old_get
        if old_key is None:
            os.environ.pop("RENTCAST_API_KEY", None)
        else:
            os.environ["RENTCAST_API_KEY"] = old_key


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
