"""RentCast adapter — a licensed/compliant sale-listings source (has a free tier).

Status: REAL code, UNTESTED against the live API (no key in this environment). The
request shape and field mapping follow RentCast's documented `/listings/sale` schema
but must be verified against your account once RENTCAST_API_KEY is set. Wire it in by:
  1. sign up at rentcast.io, put the key in .env as RENTCAST_API_KEY
  2. run: python run_fetch.py --source rentcast
  3. confirm the _normalize() mapping matches the payload you get back.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Optional

import requests

from .base import CredentialError, SourceAdapter

BASE_URL = "https://api.rentcast.io/v1"

# RentCast propertyType -> Kash property_type. Extend as you see real values.
_TYPE_MAP = {
    "Single Family": "sf_detached",
    "Townhouse": "sf_attached",
    "Condo": "condo",
    "Multi-Family": "2fam_detached",
    "Apartment": "condo",
}


class RentCastAdapter(SourceAdapter):
    name = "rentcast"

    def _key(self) -> str:
        key = self.config.get("api_key") or os.environ.get("RENTCAST_API_KEY")
        if not key:
            raise CredentialError(
                "RentCastAdapter needs RENTCAST_API_KEY (env or config). "
                "Get one at rentcast.io and add it to .env."
            )
        return key

    def fetch(self, preferences: dict) -> list[dict]:
        key = self._key()
        # Every ZIP costs one API call against the free tier's 50/month. The config override
        # (sources.rentcast.zips) keeps this source on the 6 core ZIPs after the search
        # allow-list widened to 11 — 11 weekly would run ~47 calls/month, where one backoff
        # retry breaches the quota and starts the 403 spiral the schedule backoff exists to
        # prevent. Zillow covers the added ZIPs nightly.
        zips = self.config.get("zips") or preferences.get("zips") or []
        limit = int(self.config.get("results_limit") or preferences.get("fetch_limit", 100))
        out: list[dict] = []
        for zip_code in zips:
            # /listings/sale supports location + status + limit; price is filtered
            # downstream by the pipeline's scope check, not by the API.
            params = {
                "zipCode": zip_code,
                "status": "Active",
                "limit": limit,
            }
            resp = requests.get(
                f"{BASE_URL}/listings/sale",
                params=params,
                headers={"X-Api-Key": key, "Accept": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            for raw in resp.json() or []:
                out.append(self._stamp(self._normalize(raw)))
        return out

    def _normalize(self, raw: dict) -> dict:
        listed = raw.get("listedDate")
        return {
            "street_address": raw.get("addressLine1") or raw.get("formattedAddress"),
            "zip": str(raw.get("zipCode") or "") or None,
            "list_price": raw.get("price"),
            "property_type": _TYPE_MAP.get(raw.get("propertyType"), raw.get("propertyType")),
            "beds": _as_str(raw.get("bedrooms")),
            "baths": _as_str(raw.get("bathrooms")),
            "sqft": raw.get("squareFootage"),
            "year_built": raw.get("yearBuilt"),
            "lot_size_sqft": raw.get("lotSize"),
            "days_on_market": raw.get("daysOnMarket"),
            "listing_date": _iso_date(listed),
            "mls_number": raw.get("mlsNumber"),
            "latitude": raw.get("latitude"),
            "longitude": raw.get("longitude"),
            "listing_agent": _dig(raw, "listingAgent", "name"),
            "listing_brokerage": _dig(raw, "listingOffice", "name"),
            "status": "active",
            "listing_url": raw.get("listingUrl"),
            "source_url": raw.get("listingUrl"),
            "fetched_at": date.today().isoformat(),
        }


def _as_str(v) -> Optional[str]:
    return None if v is None else str(v)


def _dig(d: dict, *path):
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _iso_date(v) -> Optional[str]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(
            timezone.utc
        ).date().isoformat()
    except ValueError:
        return None
