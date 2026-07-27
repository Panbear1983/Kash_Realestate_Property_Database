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
from typing import Optional
from urllib.parse import urlparse

import requests

from ..dedup import match_key
from ..signals import priority_note

ACTOR = "maxcopell~zillow-detail-scraper"
RUN = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"

_HOME_TYPE = {
    "SINGLE_FAMILY": "sf_detached", "TOWNHOUSE": "sf_attached",
    "MULTI_FAMILY": "2fam_detached", "CONDO": "condo", "APARTMENT": "condo", "LOT": "lot",
}


def _zpid(url: str):
    m = re.search(r"(\d+)_zpid", url or "")
    return m.group(1) if m else None


def _slug(url: str) -> str:
    """The address portion of a Zillow URL, normalised for matching.

    Both URL shapes carry it:
        /homes/109-Cardiff-St-Staten-Island-NY-10312/
        /homedetails/45-Fairlawn-Loop-Staten-Island-NY-10308/32346621_zpid/
    """
    m = re.search(r"/home(?:s|details)/([^/]+)/", url or "")
    return re.sub(r"[^a-z0-9]", "", m.group(1).lower()) if m else ""


def _needs_detail(r: dict, prefs: Optional[dict] = None) -> bool:
    """Any Zillow listing not yet detailed (year_built as the sentinel).

    Previously this required a zpid, which excluded every `/homes/<address>/` URL — 33 of the
    35 hand-curated rows and 15 of the 18 currently queued for alerting. Those are exactly the
    buyer's shortlist, so the richest listings in the pool were the only ones that could never
    have their description, taxes or year built filled in. The Apify detail actor resolves
    address URLs perfectly well; only our filter was refusing them.
    """
    u = r.get("listing_url") or ""
    parsed = urlparse(u)
    host = (parsed.hostname or "").lower()
    is_zillow = host == "zillow.com" or host.endswith(".zillow.com")
    if not (parsed.scheme == "https" and is_zillow and _slug(u) and not r.get("year_built")):
        return False
    if prefs:
        from ..notifications import in_alert_scope
        return in_alert_scope(r, prefs)   # don't spend Apify credit on out-of-scope rows
    return True


def enrich_details(store, limit: int = 15, prefs: Optional[dict] = None) -> dict:
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        return {"detailed": 0, "skipped": "no APIFY_TOKEN"}
    rows = [r for r in store.all() if _needs_detail(r, prefs)]
    if limit:
        rows = rows[:limit]
    if not rows:
        return {"detailed": 0, "candidates": 0}

    # Match returned items back by zpid where we have one, and by address slug otherwise —
    # a row sent as /homes/<address>/ has no zpid to match on until the actor tells us one.
    zmap, smap = {}, {}
    start = []
    for r in rows:
        url = r.get("listing_url") or ""
        key = match_key(r)
        z = _zpid(url)
        if z:
            zmap[z] = key
        if _slug(url):
            smap[_slug(url)] = key
        start.append({"url": url})

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
        returned_url = it.get("url") or it.get("hdpUrl") or ""
        z = str(it.get("zpid") or "") or _zpid(returned_url)
        key = zmap.get(z) or smap.get(_slug(returned_url))
        if not key:
            continue
        upd = {k: v for k, v in _normalize(it).items() if v not in (None, "")}
        # Upgrade an address URL to the canonical homedetails one so later runs match by zpid
        # directly, and so the link in Telegram points at the real listing page.
        if z and returned_url and _zpid(returned_url):
            upd["listing_url"] = returned_url
        if upd:
            store.update_fields(key, upd)
            n += 1
    return {"detailed": n, "candidates": len(rows)}


def _price_history(ph):
    out = [{"date": e.get("date"), "price": e.get("price"), "event": e.get("event")}
           for e in (ph or [])[:12]]
    return out or None


def _original_list(ph):
    """The asking price at the start of the CURRENT listing cycle.

    priceHistory is newest-first, so this takes listings[0]. It previously took listings[-1],
    the oldest 'Listed for sale' event in the whole history — which for a home sold once before
    is a previous owner's price from years ago. 203 Chandler Ave stored $525,000 from a 2011
    listing against a $915,000 asking price, and that value is exported to CSV and rendered in
    the dashboard looking like clean data. It also feeds price_drop_pct, so a decade of market
    appreciation was being reported as a price cut.
    """
    listings = [e for e in (ph or []) if (e.get("event") or "").lower().startswith("listed")]
    return listings[0].get("price") if listings else None


def _normalize(it: dict) -> dict:
    rf = it.get("resoFacts") or {}
    ai = it.get("attributionInfo") or {}
    ht = it.get("homeType")
    ph = it.get("priceHistory") or []
    lot = it.get("lotAreaValue") or it.get("lotSize")
    description = (it.get("description") or "")[:1000] or None
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
        "listing_description": description,
        "priority_note": priority_note(description),
        "last_sold_price": it.get("lastSoldPrice"),
        "original_list_price": _original_list(ph),
        "price_history": _price_history(ph),
        "property_type": _HOME_TYPE.get(ht, (ht or "").lower() or None),
        "is_multifamily": (ht or "").startswith("MULTI"),
    }
