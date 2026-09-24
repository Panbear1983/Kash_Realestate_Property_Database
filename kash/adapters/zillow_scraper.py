"""Zillow adapter via the Apify actor `maxcopell/zillow-scraper` — the FLAGGED source.

⚠️  ToS NOTE: Zillow's Terms of Service prohibit scraping and Zillow runs active
anti-bot defenses. This adapter does not hit Zillow directly — it delegates to Apify's
managed actor, a gray-area path you accepted in the ROADMAP ("hybrid" sourcing). Prefer
the licensed RentCast source; use this for Zillow-only coverage, at a low cadence.

Setup:
  1. Apify account (free) -> Settings -> API token (apify_api_...).
  2. Put it in .env as APIFY_TOKEN=apify_api_...
  3. Verify cheaply first:  python run_fetch.py --source zillow --limit 5
     (resultsLimit is PER search URL, and you have 6 ZIPs — keep it small while testing
      so you don't burn the $5/month Apify free credit.)

Input schema (per the actor docs): searchUrls[], extractionMethod, resultsLimit.

Output format history — the actor renames its fields without notice:
  * until 2026-09-01: flat Zillow "card" fields — addressStreet/addressZipcode,
    unformattedPrice, beds, baths, area, latLong{latitude,longitude}, detailUrl, imgSrc,
    statusType, brokerName, variableData, hdpData.homeInfo{...}.
  * 2026-09-01 run: both shapes side by side. From 2026-09-02: only the nested shape —
    listingAddress{street,zipCode}, listingPrice{amount}, bedrooms, bathrooms, livingArea,
    propertyUrl, coordinates{latitude,longitude}, listingStatus ("forSale"), listingType
    {isComingSoon,...}, broker{name}, mainImage, photoCount, daysOnZillow, zpid.
  Four nights of results were silently rejected for lack of an address before the change
  was noticed, so `_normalize` reads the new shape first and falls back to the old, and
  kash/health.py flags a source whose results are all rejected.

Replay: set ZILLOW_REPLAY_DATASET=<apify dataset id> (or config `replay_dataset`) and
fetch() reads that stored run's items instead of starting the actor. Apify keeps every
run's dataset and reading one is free; only actor runs bill. Use it to re-ingest a night
whose results were mis-read, or to check a mapping change against real output.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from datetime import date
from typing import Optional

import requests

from .base import CredentialError, SourceAdapter

APIFY_RUN_URL = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
APIFY_DATASET_URL = "https://api.apify.com/v2/datasets/{dataset}/items"
ZILLOW = "https://www.zillow.com"

_HOME_TYPE = {
    "SINGLE_FAMILY": "sf_detached",
    "TOWNHOUSE": "sf_attached",
    "MULTI_FAMILY": "2fam_detached",
    "CONDO": "condo",
    "APARTMENT": "condo",
    "LOT": "lot",
}
_STATUS = {
    "FOR_SALE": "active",
    "PENDING": "pending",
    "SOLD": "sold",
    "COMING_SOON": "active",
}
# The 2026-09 shape's `listingStatus` (camelCase). Compared lower-cased with separators
# stripped, so "forSale", "FOR_SALE" and "for_sale" all land on the same row.
_STATUS_NEW = {
    "forsale": "active",
    "comingsoon": "active",
    "pending": "pending",
    "undercontract": "pending",
    "sold": "sold",
    "recentlysold": "sold",
    "offmarket": "off_market",
}


# Staten Island map bounds. The actor's PAGINATION_WITH_ZOOM_IN subdivides this into
# quadrants; a plain per-ZIP searchQueryState (no bounds) returns "No results found".
# NOTE: the western edge slightly overlaps NJ — the pipeline's zip_prefix scope filter
# drops off-island results (e.g. Carteret, NJ 07008).
SI_BOUNDS = {"north": 40.651, "south": 40.477, "east": -74.052, "west": -74.259}


# excluded_property_types (preferences) -> Zillow filterState toggles. Filtering in the
# query matters because resultsLimit caps the run at N results BEFORE our scope filter
# runs: with no type/beds filter, 35-78% of the 40 slots went to listings the pipeline
# immediately discarded, and the newest in-scope listings fell outside the window.
_EXCLUDE_FILTERS = {
    "condo": ("isCondo", "isApartmentOrCondo"),
    "apartment": ("isApartment", "isApartmentOrCondo"),
    "lot": ("isLotLand",),
    "land": ("isLotLand",),      # RentCast's spelling; same Zillow toggle as `lot`
    "manufactured": ("isManufactured",),
}


def _search_url(term: str, bounds: dict, price: dict | None = None,
                beds_min=None, baths_min=None, excluded_types=(),
                max_days_on_market=None, include_other_listings=False) -> str:
    """A Zillow for-sale search URL with map bounds (+ optional price slice for the
    rotating sweep), carrying the required ?searchQueryState= parameter."""
    filter_state = {"sortSelection": {"value": "days"}}
    if max_days_on_market:
        # Zillow's "days on Zillow" filter (accepted values 1/7/14/30/90). With it, the
        # query returns ONLY recently-listed homes — discovery without re-buying results
        # the RentCast census re-sights for free. Verified live 2026-08-16: a doz=7 probe
        # returned exclusively daysOnZillow=1 items.
        filter_state["doz"] = {"value": str(int(max_days_on_market))}
    if include_other_listings:
        # Explicitly include owner-listed (FSBO) and coming-soon homes alongside agent
        # listings — segments Zillow's default view can tuck under "Other listings" and
        # the MLS-fed census structurally cannot see. Actor-accepted (probe 2026-08-16:
        # returned a live is_comingSoon item).
        for toggle in ("fsba", "fsbo", "cmsn"):
            filter_state[toggle] = {"value": True}
    if price and (price.get("min") or price.get("max")):
        pf = {}
        if price.get("min"):
            pf["min"] = int(price["min"])
        if price.get("max"):
            pf["max"] = int(price["max"])
        filter_state["price"] = pf
    if beds_min:
        filter_state["beds"] = {"min": int(beds_min)}
    if baths_min:
        # Zillow accepts fractional bath minimums; keep whole numbers as ints.
        b = float(baths_min)
        filter_state["baths"] = {"min": int(b) if b == int(b) else b}
    for t in excluded_types:
        for key in _EXCLUDE_FILTERS.get(str(t).lower(), ()):
            filter_state[key] = {"value": False}
    sqs = {
        "usersSearchTerm": term,
        "mapBounds": bounds,
        "filterState": filter_state,
        "isListVisible": True,
        "isMapVisible": True,
    }
    q = urllib.parse.quote(json.dumps(sqs, separators=(",", ":")))
    return f"{ZILLOW}/homes/for_sale/?searchQueryState={q}"


class ZillowScraperAdapter(SourceAdapter):
    name = "zillow"

    def fetch(self, preferences: dict) -> list[dict]:
        token = self.config.get("apify_token") or os.environ.get("APIFY_TOKEN")
        if not token:
            raise CredentialError(
                "ZillowScraperAdapter needs an Apify token. Set APIFY_TOKEN in .env. "
                "This source is flagged: Zillow ToS prohibits direct scraping."
            )
        actor = self.config.get("apify_actor") or os.environ.get(
            "APIFY_ACTOR", "maxcopell~zillow-scraper"
        )
        search = preferences.get("search") or {}
        term = search.get("term") or preferences.get("market") or "Staten Island, NY"
        bounds = search.get("bounds") or SI_BOUNDS
        results_limit = int(self.config.get("results_limit") or preferences.get("fetch_limit", 100))
        price = None
        if self.config.get("price_min") or self.config.get("price_max"):
            price = {"min": self.config.get("price_min"), "max": self.config.get("price_max")}
        eligibility = preferences.get("eligibility") or {}
        filters = dict(
            beds_min=preferences.get("beds_min"),
            baths_min=eligibility.get("store_min_baths"),
            excluded_types=eligibility.get("excluded_property_types") or (),
            max_days_on_market=self.config.get("max_days_on_market"),
            include_other_listings=bool(self.config.get("include_fsbo_coming_soon")),
        )
        # Two queries per night, one actor run. resultsLimit is PER search URL, and the
        # actor charges per returned item, so the nightly bound is len(urls) x limit.
        #   1. "freshness": the full preference price range, newest first — a new listing is
        #      seen the night it posts instead of waiting for its band's turn (up to 2 days).
        #   2. "depth": the rotating sweep band — re-sightings that catch price drops.
        # The band URL is skipped when it equals the full range (or no band was configured),
        # so run_fetch and single-range setups keep their one-URL behavior and cost.
        full_price = preferences.get("price") or None
        urls = [_search_url(term, bounds, full_price, **filters)]
        if price:
            band_url = _search_url(term, bounds, price, **filters)
            if band_url != urls[0]:
                urls.append(band_url)
        # Remember what was asked for, so coverage() can say what this run proves. The claim
        # is always the FULL preference range: the freshness URL spans it in every case (a
        # lone band URL only happens when the band IS the full range), so a run that comes
        # back under the cap covered the whole claimed slice regardless of the band.
        claimed = full_price
        self._query = {
            "source": self.name,
            "price_min": (claimed or {}).get("min"),
            "price_max": (claimed or {}).get("max"),
            "beds_min": preferences.get("beds_min"),
            "baths_min": eligibility.get("store_min_baths"),
            "excluded_types": list(eligibility.get("excluded_property_types") or ()),
            "zips": list(preferences.get("zips") or ()),
            "results_limit": results_limit,
            "search_urls": len(urls),
            # A doz-filtered run only saw recent listings; it can never claim conclusive
            # coverage of the whole range (truncated semantics already prevent that at
            # these limits, but the claim is recorded for honesty).
            "max_days_on_market": self.config.get("max_days_on_market"),
        }
        replay = self.config.get("replay_dataset") or os.environ.get("ZILLOW_REPLAY_DATASET")
        if replay:
            # Re-ingest a run the actor already produced instead of buying a new one (see the
            # module docstring). The query above is still recorded so coverage() describes
            # what that run asked for; a replayed run is never conclusive anyway.
            self._query["replay_dataset"] = str(replay)
            resp = requests.get(
                APIFY_DATASET_URL.format(dataset=replay),
                params={"token": token, "clean": "true", "limit": 1000},
                timeout=120,
            )
        else:
            payload = {
                "searchUrls": [{"url": u} for u in urls],
                "extractionMethod": "PAGINATION_WITH_ZOOM_IN",
                "resultsLimit": results_limit,
            }
            resp = requests.post(
                APIFY_RUN_URL.format(actor=actor),
                params={"token": token},
                json=payload,
                timeout=300,
            )
        resp.raise_for_status()
        return [self._stamp(self._normalize(r)) for r in (resp.json() or [])]

    def coverage(self, fetched: int) -> Optional[dict]:
        """The slice this run searched, and whether it hit the cap.

        `resultsLimit` applies per search URL, so with two URLs `fetched` can reach twice
        the limit and `fetched >= results_limit` stays the conservative truncation test: a
        total at or past one URL's cap means at least one query may have been cut off.
        A total under it means no URL hit its cap — and since the freshness URL alone spans
        the claimed price range, the claim is complete. kash/lifecycle.py is the consumer.
        """
        q = getattr(self, "_query", None)
        if not q:
            return None
        # A doz-filtered (days-on-market) run is NEVER conclusive, whatever it fetched:
        # it only asked about recent listings, so older active listings are absent by
        # construction — treating that absence as evidence would mass-age the pool.
        truncated = fetched >= int(q["results_limit"]) or bool(q.get("max_days_on_market"))
        return {**q, "truncated": truncated}

    def _normalize(self, r: dict) -> dict:
        """One actor item -> one schema-shaped dict. New (2026-09) keys first, legacy
        keys as the fallback, so a transitional item carrying both reads the same either
        way. A missing address or ZIP leaves the row without an identity and the store
        rejects it — which is exactly what happened to every row for four nights when
        only the legacy keys were read."""
        home = (r.get("hdpData") or {}).get("homeInfo") or {}
        addr = _dict(r.get("listingAddress"))
        price = _dict(r.get("listingPrice"))
        broker = _dict(r.get("broker"))
        detail = r.get("propertyUrl") or r.get("detailUrl") or ""
        if detail.startswith("/"):
            detail = ZILLOW + detail
        ht = r.get("homeType") or home.get("homeType")
        # `listingAddress.street` already carries the unit ("523 Willow Rd E #1"), exactly
        # as the legacy addressStreet did, so the dedup key is unchanged across formats.
        return {
            "street_address": _first(addr.get("street"), r.get("addressStreet"),
                                     r.get("address"), home.get("streetAddress")),
            "zip": _as_str(_first(addr.get("zipCode"), r.get("addressZipcode"),
                                  home.get("zipcode"))),
            "list_price": _first(price.get("amount"), r.get("unformattedPrice"),
                                 home.get("price")),
            "property_type": _HOME_TYPE.get(ht, (ht or "").lower() or None),
            "beds": _as_str(_first(r.get("bedrooms"), r.get("beds"), home.get("bedrooms"))),
            "baths": _as_str(_first(r.get("bathrooms"), r.get("baths"), home.get("bathrooms"))),
            "sqft": _first(r.get("livingArea"), r.get("area"), home.get("livingArea")),
            "year_built": home.get("yearBuilt"),
            "zestimate": _first(r.get("zestimate"), home.get("zestimate")),
            "zestimate_source": "zillow",
            "estimated_rent_monthly": _first(r.get("rentZestimate"), home.get("rentZestimate")),
            "days_on_market": _days_on(r, home),
            "latitude": _first(_coord(r, "latitude"), home.get("latitude")),
            "longitude": _first(_coord(r, "longitude"), home.get("longitude")),
            "status": _status(r),
            "listing_brokerage": _first(broker.get("name"), r.get("brokerName")),
            "primary_photo_url": _first(r.get("mainImage"), r.get("imgSrc")),
            "photo_count": r.get("photoCount"),
            "listing_url": detail or None,
            "source_url": detail or None,
            "fetched_at": date.today().isoformat(),
        }


def _dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _first(*values):
    """The first value that is actually there (None and "" both count as absent)."""
    for v in values:
        if v is not None and v != "":
            return v
    return None


def _status(r: dict) -> str:
    new = r.get("listingStatus")
    if new:
        return _STATUS_NEW.get(re.sub(r"[^a-z]", "", str(new).lower()), "active")
    return _STATUS.get(r.get("statusType"), "active")


def _as_str(v) -> Optional[str]:
    return None if v is None or v == "" else str(v)


def _coord(r: dict, key: str):
    """coordinates{} (2026-09 shape) or latLong{} (legacy)."""
    ll = r.get("coordinates")
    if not isinstance(ll, dict):
        ll = r.get("latLong")
    return ll.get(key) if isinstance(ll, dict) else None


def _days_on(r: dict, home: dict) -> Optional[int]:
    if r.get("daysOnZillow") is not None:            # 2026-09 shape: top level
        return r["daysOnZillow"]
    if home.get("daysOnZillow") is not None:         # legacy: hdpData.homeInfo
        return home["daysOnZillow"]
    vd = r.get("variableData")                       # legacy: "3 days on Zillow" text
    text = vd.get("text") if isinstance(vd, dict) else None
    if text:
        digits = "".join(c for c in text if c.isdigit())
        return int(digits) if digits else None
    return None
