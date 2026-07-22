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
Output fields: addressStreet/addressCity/addressZipcode, unformattedPrice, beds, baths,
area, latLong{latitude,longitude}, zestimate, rentZestimate, detailUrl, imgSrc,
homeType, statusType, brokerName, variableData, hdpData.homeInfo.
"""
from __future__ import annotations

import json
import os
import urllib.parse
from datetime import date
from typing import Optional

import requests

from .base import CredentialError, SourceAdapter

APIFY_RUN_URL = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
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


# Staten Island map bounds. The actor's PAGINATION_WITH_ZOOM_IN subdivides this into
# quadrants; a plain per-ZIP searchQueryState (no bounds) returns "No results found".
# NOTE: the western edge slightly overlaps NJ — the pipeline's zip_prefix scope filter
# drops off-island results (e.g. Carteret, NJ 07008).
SI_BOUNDS = {"north": 40.651, "south": 40.477, "east": -74.052, "west": -74.259}


def _search_url(term: str, bounds: dict, price: dict | None = None) -> str:
    """A Zillow for-sale search URL with map bounds (+ optional price slice for the
    rotating sweep), carrying the required ?searchQueryState= parameter."""
    filter_state = {"sortSelection": {"value": "days"}}
    if price and (price.get("min") or price.get("max")):
        pf = {}
        if price.get("min"):
            pf["min"] = int(price["min"])
        if price.get("max"):
            pf["max"] = int(price["max"])
        filter_state["price"] = pf
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
        payload = {
            "searchUrls": [{"url": _search_url(term, bounds, price)}],
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

    def _normalize(self, r: dict) -> dict:
        home = (r.get("hdpData") or {}).get("homeInfo") or {}
        detail = r.get("detailUrl") or ""
        if detail.startswith("/"):
            detail = ZILLOW + detail
        ht = r.get("homeType") or home.get("homeType")
        return {
            "street_address": r.get("addressStreet") or r.get("address"),
            "zip": str(r.get("addressZipcode") or home.get("zipcode") or "") or None,
            "list_price": r.get("unformattedPrice") or home.get("price"),
            "property_type": _HOME_TYPE.get(ht, (ht or "").lower() or None),
            "beds": _as_str(r.get("beds")),
            "baths": _as_str(r.get("baths")),
            "sqft": r.get("area") or home.get("livingArea"),
            "year_built": home.get("yearBuilt"),
            "zestimate": r.get("zestimate") or home.get("zestimate"),
            "zestimate_source": "zillow",
            "estimated_rent_monthly": r.get("rentZestimate") or home.get("rentZestimate"),
            "days_on_market": _days_on(r, home),
            "latitude": _latlong(r, "latitude") or home.get("latitude"),
            "longitude": _latlong(r, "longitude") or home.get("longitude"),
            "status": _STATUS.get(r.get("statusType"), "active"),
            "listing_brokerage": r.get("brokerName"),
            "primary_photo_url": r.get("imgSrc"),
            "listing_url": detail or None,
            "source_url": detail or None,
            "fetched_at": date.today().isoformat(),
        }


def _as_str(v) -> Optional[str]:
    return None if v is None else str(v)


def _latlong(r: dict, key: str):
    ll = r.get("latLong")
    return ll.get(key) if isinstance(ll, dict) else None


def _days_on(r: dict, home: dict) -> Optional[int]:
    if home.get("daysOnZillow") is not None:
        return home["daysOnZillow"]
    vd = r.get("variableData")
    text = vd.get("text") if isinstance(vd, dict) else None
    if text:
        digits = "".join(c for c in text if c.isdigit())
        return int(digits) if digits else None
    return None
