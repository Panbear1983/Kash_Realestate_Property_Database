"""Zillow detail-scrape enrichment.

The search actor returns only summary data; the deep fields (year built, lot size, taxes,
HOA, basement, heating, agent, MLS, description, photos, price history, last sold) live on
the property's detail page. For new Zillow rows that haven't been detailed yet, this calls
the Apify Zillow detail actor with their listing URLs and fills those columns. Bounded per
run to keep credit small; matched back to rows by Zillow zpid.
"""
from __future__ import annotations

import os
import re

import requests

from ..dedup import match_key

ACTOR = "maxcopell~zillow-detail-scraper"
RUN = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"

_HOME_TYPE = {
    "SINGLE_FAMILY": "sf_detached", "TOWNHOUSE": "sf_attached",
    "MULTI_FAMILY": "2fam_detached", "CONDO": "condo", "APARTMENT": "condo", "LOT": "lot",
}


def _zpid(url: str):
    m = re.search(r"(\d+)_zpid", url or "")
    return m.group(1) if m else None


def _needs_detail(r: dict) -> bool:
    # any Zillow homedetails link (has a zpid) not yet detailed (year_built as sentinel)
    u = r.get("listing_url") or ""
    return bool("zillow.com" in u and _zpid(u) and not r.get("year_built"))


def enrich_details(store, limit: int = 15) -> dict:
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        return {"detailed": 0, "skipped": "no APIFY_TOKEN"}
    rows = [r for r in store.all() if _needs_detail(r)]
    if limit:
        rows = rows[:limit]
    if not rows:
        return {"detailed": 0, "candidates": 0}

    zmap = {}
    start = []
    for r in rows:
        z = _zpid(r.get("listing_url"))
        if z:
            zmap[z] = match_key(r)
        start.append({"url": r["listing_url"]})

    try:
        resp = requests.post(
            RUN.format(actor=ACTOR), params={"token": token},
            json={"startUrls": start, "propertyStatus": "FOR_SALE"}, timeout=600,
        )
        resp.raise_for_status()
        items = resp.json() or []
    except Exception as e:  # noqa: BLE001
        return {"detailed": 0, "error": str(e)}

    n = 0
    for it in items:
        if not isinstance(it, dict) or "error" in it:
            continue
        z = str(it.get("zpid") or "") or _zpid(it.get("url") or it.get("hdpUrl") or "")
        key = zmap.get(z)
        if not key:
            continue
        upd = {k: v for k, v in _normalize(it).items() if v not in (None, "")}
        if upd:
            store.update_fields(key, upd)
            n += 1
    return {"detailed": n, "candidates": len(rows)}


def _price_history(ph):
    out = [{"date": e.get("date"), "price": e.get("price"), "event": e.get("event")}
           for e in (ph or [])[:12]]
    return out or None


def _original_list(ph):
    listings = [e for e in (ph or []) if (e.get("event") or "").lower().startswith("listed")]
    return listings[-1].get("price") if listings else None   # ph is newest-first


def _normalize(it: dict) -> dict:
    rf = it.get("resoFacts") or {}
    ai = it.get("attributionInfo") or {}
    ht = it.get("homeType")
    ph = it.get("priceHistory") or []
    lot = it.get("lotAreaValue") or it.get("lotSize")
    return {
        "year_built": it.get("yearBuilt") or rf.get("yearBuilt"),
        "lot_size_sqft": int(lot) if lot else None,
        "property_tax_annual": rf.get("taxAnnualAmount"),
        "hoa_monthly": it.get("monthlyHoaFee") or rf.get("hoaFee"),
        "basement": (rf.get("basement") or "").lower() or None,
        "garage_spaces": rf.get("garageSpaces"),
        "heating": ", ".join(rf.get("heating") or []) or None,
        "cooling": ", ".join(rf.get("cooling") or []) or None,
        "mls_number": ai.get("mlsId"),
        "listing_agent": ai.get("agentName"),
        "photo_count": it.get("photoCount"),
        "listing_description": (it.get("description") or "")[:1000] or None,
        "last_sold_price": it.get("lastSoldPrice"),
        "original_list_price": _original_list(ph),
        "price_history": _price_history(ph),
        "property_type": _HOME_TYPE.get(ht, (ht or "").lower() or None),
        "is_multifamily": (ht or "").startswith("MULTI"),
    }
