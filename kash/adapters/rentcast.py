"""RentCast adapter — the pipeline's re-sighting engine (licensed API, free tier).

RentCast bills per REQUEST (50/month free), not per record, which makes it the one source
where re-checking everything costs nothing: the default "city" mode pulls the entire
filtered Staten Island market in a single call (limit 500; live probe 2026-08-16 measured
460 matching actives) with server-side `price` and `bedrooms` range filters. Run every 2
days that is ~16 of the 50 monthly calls, and every price drop in the pool is caught
within two days — Zillow's nightly job stays discovery-only (new listings), so the paid
per-result actor never re-buys rows this census re-sights free.

City mode also reports conclusive coverage (kash/lifecycle.py): a census that came back
complete proves which rentcast-sourced rows are no longer listed. The legacy per-ZIP mode
(`sources.rentcast.mode: zips`) remains as a fallback — each ZIP is one API call, so if
you switch to it, restore `every_days: 7` (per-ZIP at the census cadence would burn ~93
calls/month against the 50-call quota).
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Optional

import requests

from .base import CredentialError, SourceAdapter

log = logging.getLogger("kash.rentcast")

BASE_URL = "https://api.rentcast.io/v1"

# The census never makes more than this many API calls per run. Needing a third page would
# mean 1000+ matching listings — i.e. the server-side price filter is being ignored — and
# paging on would burn the monthly quota re-fetching an unfiltered borough.
MAX_PAGES = 2

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
        if str(self.config.get("mode") or "city").lower() == "zips":
            return self._fetch_zips(key, preferences)
        return self._fetch_city(key, preferences)

    def _fetch_city(self, key: str, preferences: dict) -> list[dict]:
        """One census call for the whole city, server-side filtered to the buyer's slice."""
        # Page size is NOT prefs.fetch_limit (100): that would quietly turn the one-call
        # census into a 3-4 call paging loop. 500 is the API maximum.
        limit = int(self.config.get("results_limit") or 500)
        params = {
            "city": self.config.get("city") or "Staten Island",   # case-sensitive per docs
            "state": self.config.get("state") or "NY",
            "status": "Active",
            "limit": limit,
            "includeTotalCount": "true",   # the string — Python True urlencodes as 'True'
        }
        price = preferences.get("price") or {}
        lo, hi = price.get("min"), price.get("max")
        if lo or hi:
            params["price"] = f"{int(lo) if lo else '*'}:{int(hi) if hi else '*'}"
        beds_min = preferences.get("beds_min")
        if beds_min:
            params["bedrooms"] = f"{int(beds_min)}:*"
        # Deliberately NO bathrooms/propertyType server filters: numeric API filters drop
        # null-valued records, and bath-less rows must keep landing in the completeness
        # "blocked from alerting" tally instead of silently vanishing. Type is policy,
        # enforced at ingest by eligibility.

        out: list[dict] = []
        total_count, pages, error, last_len = None, 0, None, None
        while pages < MAX_PAGES:
            params["offset"] = len(out)
            try:
                resp = requests.get(
                    f"{BASE_URL}/listings/sale",
                    params=params,
                    headers={"X-Api-Key": key, "Accept": "application/json"},
                    timeout=30,
                )
                resp.raise_for_status()
                page = resp.json() or []
            except (requests.RequestException, ValueError) as exc:
                if not out:
                    raise      # first page dead: fail the source -> schedule backoff
                # Mid-pagination: keep the salvaged records, claim nothing below.
                error = str(exc)
                log.warning("census page %d failed after %d records: %s",
                            pages + 1, len(out), exc)
                break
            pages += 1
            if total_count is None:
                try:
                    total_count = int(resp.headers.get("X-Total-Count") or "")
                except (TypeError, ValueError):
                    total_count = None
            for raw in page:   # outside the try: a mapping bug must surface as an error
                out.append(self._stamp(self._normalize(raw)))
            last_len = len(page)
            if last_len < limit:
                break          # short page: everything arrived
            if total_count is not None and len(out) >= total_count:
                break          # exact boundary: skip the empty confirming call

        # An EMPTY census is never conclusive: 460 matching actives were measured when
        # this shipped, so zero rows means a broken query (city typo, filter regression),
        # and claiming completeness would start ageing rows on the strength of a bug.
        complete = bool(out) and error is None and last_len is not None and (
            last_len < limit or (total_count is not None and len(out) >= total_count))
        # What this run proves, for lifecycle ageing. Empty zips/excluded_types = "no
        # constraint": the city spans every pool ZIP and no type filter was applied.
        self._census = {
            "source": self.name,
            "price_min": int(lo) if lo else None,
            "price_max": int(hi) if hi else None,
            "beds_min": int(beds_min) if beds_min else None,
            "excluded_types": [],
            "zips": [],
            "results_limit": limit,
            "pages": pages,
            "total_count": total_count,
            "error": error,
            "truncated": not complete,
        }
        return out

    def coverage(self, fetched: int) -> Optional[dict]:
        """City-mode census coverage; None before any fetch and always None in zips mode
        (a capped per-ZIP window never proves absence)."""
        return getattr(self, "_census", None)

    def _fetch_zips(self, key: str, preferences: dict) -> list[dict]:
        """Legacy per-ZIP mode — one API call PER ZIP. If you run this mode, keep the
        weekly cadence: the census cadence (every 2 days) across 11 ZIPs would burn ~93
        of the 50 monthly calls."""
        zips = self.config.get("zips") or preferences.get("zips") or []
        limit = int(self.config.get("results_limit") or preferences.get("fetch_limit", 100))
        out: list[dict] = []
        for zip_code in zips:
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
