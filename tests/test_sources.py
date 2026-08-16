#!/usr/bin/env python3
"""Source-registry contract tests; no provider credentials or network required."""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import preferences  # noqa: E402
from kash.adapters import REGISTRY  # noqa: E402


def _seed_csv(path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["zip", "property_type", "neighborhood", "list_price"])
        writer.writeheader()
        writer.writerow({"zip": "10308", "property_type": "sf_detached",
                         "neighborhood": "Great Kills", "list_price": "700000"})


def test_derived_sources_are_implemented_adapters():
    with tempfile.TemporaryDirectory() as tmp:
        seed = os.path.join(tmp, "seed.csv")
        _seed_csv(seed)
        prefs = preferences.derive_from_csv(seed)
    assert set(prefs["sources"]) <= set(REGISTRY), prefs["sources"]


def test_manual_source_selection_rejects_unsupported_provider_names():
    try:
        preferences.validate_source_names(["rentcast", "redfin"], REGISTRY)
    except ValueError as exc:
        assert "redfin" in str(exc)
    else:
        raise AssertionError("unsupported source must be rejected before scheduling")


# --- the Zillow search URL carries the buyer's filters -------------------------------------
# resultsLimit caps the run BEFORE our scope filter sees anything, so every filter missing
# from the query wastes result slots on listings the pipeline discards (measured: 35-78%).

def _decoded_filter_state(**kwargs):
    import json
    import urllib.parse
    from kash.adapters.zillow_scraper import SI_BOUNDS, _search_url
    url = _search_url("Staten Island, NY", SI_BOUNDS, **kwargs)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["searchQueryState"][0]
    return json.loads(q)["filterState"]


def test_search_url_carries_beds_baths_and_price():
    fs = _decoded_filter_state(price={"min": 560915, "max": 640915},
                               beds_min=3, baths_min=2)
    assert fs["beds"] == {"min": 3}
    assert fs["baths"] == {"min": 2}, "whole-number baths must not serialize as 2.0"
    assert fs["price"] == {"min": 560915, "max": 640915}
    assert fs["sortSelection"] == {"value": "days"}


def test_search_url_excludes_the_rejected_property_types():
    fs = _decoded_filter_state(excluded_types=["condo", "apartment", "lot"])
    for key in ("isCondo", "isApartment", "isApartmentOrCondo", "isLotLand"):
        assert fs[key] == {"value": False}, key


def test_unknown_excluded_type_is_ignored_not_fatal():
    fs = _decoded_filter_state(excluded_types=["houseboat"])
    assert "houseboat" not in str(fs)


def test_bare_search_url_is_unchanged():
    fs = _decoded_filter_state()
    assert set(fs) == {"sortSelection"}, "no prefs -> no filters, as before"


# --- what a run proves about what it did NOT return (kash/lifecycle.py) ---------------------

FULL_RANGE = {"min": 560915, "max": 900000}
BAND = {"price_min": 560915, "price_max": 640915}


def _fetch_with(n_results, config=None, prefs_over=None):
    """Run the Zillow adapter against a faked actor returning n results.

    Returns (adapter, rows, posted_payload) so tests can assert on the searchUrls the
    actor would have received.
    """
    import os
    from kash.adapters import zillow_scraper as zs

    prefs = {"market": "Staten Island, NY", "zips": ["10308"], "beds_min": 3,
             "price": dict(FULL_RANGE),
             "eligibility": {"store_min_baths": 2,
                             "excluded_property_types": ["condo", "lot"]}}
    prefs.update(prefs_over or {})
    item = {"addressStreet": "1 Test Ave", "addressZipcode": "10308",
            "unformattedPrice": 700000, "beds": 3, "baths": 2, "homeType": "SINGLE_FAMILY"}
    posted = {}

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return [dict(item) for _ in range(n_results)]

    def fake_post(url, params=None, json=None, timeout=None):
        posted.update(json)
        return R()

    old_post, old_token = zs.requests.post, os.environ.get("APIFY_TOKEN")
    zs.requests.post = fake_post
    os.environ["APIFY_TOKEN"] = "test-token"
    try:
        ad = zs.ZillowScraperAdapter(
            config=config if config is not None else {"results_limit": 40, **BAND})
        rows = ad.fetch(prefs)
        return ad, rows, posted
    finally:
        zs.requests.post = old_post
        if old_token is None:
            os.environ.pop("APIFY_TOKEN", None)
        else:
            os.environ["APIFY_TOKEN"] = old_token


def _price_of(search_url_entry):
    import json
    import urllib.parse
    q = urllib.parse.parse_qs(urllib.parse.urlparse(search_url_entry["url"]).query)
    return json.loads(q["searchQueryState"][0])["filterState"].get("price")


def test_a_nightly_run_posts_freshness_and_band_urls():
    """Two queries, one actor run: the full range newest-first (a new listing is seen the
    night it posts) plus the rotating band (re-sightings that catch price drops)."""
    _, _, posted = _fetch_with(5)
    urls = posted["searchUrls"]
    assert len(urls) == 2
    assert _price_of(urls[0]) == {"min": 560915, "max": 900000}, "freshness spans the range"
    assert _price_of(urls[1]) == {"min": 560915, "max": 640915}, "depth is tonight's band"
    assert posted["resultsLimit"] == 40, "the limit is per URL, not split between them"


def test_a_band_equal_to_the_full_range_posts_one_url():
    _, _, posted = _fetch_with(
        5, config={"results_limit": 40, "price_min": 560915, "price_max": 900000})
    assert len(posted["searchUrls"]) == 1, "identical queries must not be charged twice"


def test_no_band_posts_one_full_range_url():
    ad, rows, posted = _fetch_with(5, config={"results_limit": 40})
    assert len(posted["searchUrls"]) == 1
    assert _price_of(posted["searchUrls"][0]) == {"min": 560915, "max": 900000}
    assert ad.coverage(len(rows))["search_urls"] == 1


def test_a_run_that_hits_the_cap_reports_itself_as_truncated():
    ad, rows, _ = _fetch_with(40)
    assert ad.coverage(len(rows))["truncated"] is True, \
        "40 of a possible 40 says nothing about listing 41"


def test_a_run_at_twice_the_limit_is_still_truncated():
    """With two URLs, fetched can reach 2x the per-URL limit."""
    ad, rows, _ = _fetch_with(80)
    assert ad.coverage(len(rows))["truncated"] is True


def test_a_run_under_the_cap_reports_the_slice_it_covered():
    ad, rows, _ = _fetch_with(7)
    cov = ad.coverage(len(rows))
    assert cov["truncated"] is False
    assert cov["source"] == "zillow"
    # The claim is the FULL preference range: the freshness URL alone spans it, so a run
    # under the cap covered the whole claimed slice regardless of the band URL.
    assert (cov["price_min"], cov["price_max"]) == (560915, 900000)
    assert cov["search_urls"] == 2
    assert cov["beds_min"] == 3 and cov["baths_min"] == 2
    assert cov["zips"] == ["10308"] and "condo" in cov["excluded_types"]


def test_an_adapter_that_has_not_fetched_reports_no_coverage():
    """RentCast's city census CAN prove absence now — but only after a fetch has actually
    established what was asked; before that it must claim nothing."""
    from kash.adapters.rentcast import RentCastAdapter
    assert RentCastAdapter(config={}).coverage(600) is None


def test_the_doz_filter_lands_in_the_query_when_configured():
    """max_days_on_market -> Zillow's days-on-Zillow filter: discovery asks only about
    recent listings instead of re-buying results the census re-sights for free."""
    fs = _decoded_filter_state(max_days_on_market=7)
    assert fs["doz"] == {"value": "7"}
    assert "doz" not in _decoded_filter_state(), "absent config, absent filter"


def test_other_listings_toggles_appear_only_when_configured():
    """FSBO + coming-soon live under Zillow's 'Other listings' tab; the census cannot see
    them, so discovery must ask explicitly — but only when the config key says so."""
    fs = _decoded_filter_state(include_other_listings=True)
    for toggle in ("fsba", "fsbo", "cmsn"):
        assert fs[toggle] == {"value": True}, toggle
    fs_off = _decoded_filter_state()
    for toggle in ("fsba", "fsbo", "cmsn"):
        assert toggle not in fs_off, "absent config must leave Zillow's defaults alone"


def test_the_land_exclusion_reaches_the_query():
    fs = _decoded_filter_state(excluded_types=["land"])
    assert fs["isLotLand"] == {"value": False}, \
        "RentCast's 'land' spelling must map to the same toggle as 'lot'"


def test_a_doz_filtered_run_is_never_conclusive():
    """It only asked about recent listings — older active listings are absent by
    construction, and treating that as evidence would mass-age the pool."""
    ad, rows, _ = _fetch_with(7, config={"results_limit": 40, "max_days_on_market": 7})
    cov = ad.coverage(len(rows))
    assert cov["truncated"] is True, "7 < 40 must NOT read as complete coverage"
    assert cov["max_days_on_market"] == 7
    # and without the doz key, the same fetch count stays conclusive
    ad2, rows2, _ = _fetch_with(7, config={"results_limit": 40})
    assert ad2.coverage(len(rows2))["truncated"] is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — source config, Zillow query shape, run coverage")
