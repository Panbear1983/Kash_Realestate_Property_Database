"""A usable link for listings whose source provides none — so they can alert at all.

RentCast is a data API, not a listings site: its records carry no listing URL, and a
Telegram brief without a link is useless, so 38 active rows sat permanently "blocked from
alerting". This fills the gap with a human-usable Zillow address-search URL in the pool's
own curated style (`/homes/45-Fairlawn-Loop-Staten-Island-NY-10308/`).

Deliberately store-side, NOT in the RentCast adapter: an adapter-emitted URL would be
re-asserted on every weekly fetch and fight the merge. Done here, the lifecycle is clean —
`Store.upsert` skips None incoming values, so RentCast can never clobber the fill back to
NULL, and a real `/homedetails/…_zpid/` URL from a later Zillow sighting (non-None, differs)
overwrites it organically. `detail._zpid()` returns None for the synthesized form, so the
detail actor — which rejects address URLs — never receives one; the row simply stays in its
`awaiting_zpid_url` queue.

Rows the admission policy excludes (e.g. land lots) are skipped: they can never alert, so a
link would only dress up a row that is out of policy, not blocked.
"""
from __future__ import annotations

import re
from typing import Optional

from . import eligibility

ZILLOW = "https://www.zillow.com"
CITY_SLUG = "Staten-Island-NY"


def synthesize_url(street_address, zip_code) -> Optional[str]:
    """`45 Fairlawn Loop` + `10308` -> the pool's curated URL style, or None if either
    part is missing. A slightly-off slug still lands on a Zillow search page — degraded
    to a search, never to a 404 on some other house."""
    address = " ".join(str(street_address or "").split())
    zip_code = str(zip_code or "").strip()
    if not address or not zip_code:
        return None
    slug = re.sub(r"[^A-Za-z0-9]+", "-", address).strip("-")
    if not slug:
        return None
    return f"{ZILLOW}/homes/{slug}-{CITY_SLUG}-{zip_code}/"


def fill_missing_urls(store, prefs: dict) -> dict:
    """Fill listing_url on active, policy-admitted rows that have none. Idempotent —
    a second run finds nothing empty and writes nothing."""
    stats = {"filled": 0, "skipped": 0}
    keys = [r[0] for r in store.conn.execute(
        "SELECT match_key FROM listings"
        " WHERE status='active' AND (listing_url IS NULL OR listing_url='')").fetchall()]
    for key in keys:
        row = store.get(key)
        if row is None:
            continue
        url = synthesize_url(row.get("street_address"), row.get("zip"))
        if url is None or not eligibility.classify(row, prefs).admit:
            stats["skipped"] += 1
            continue
        if store.update_fields(key, {"listing_url": url}):
            store._log(key, "url_synthesized",
                       f"{row.get('street_address')}: search URL filled pending zpid",
                       "urlfill")
            stats["filled"] += 1
    store.conn.commit()
    return stats


def format_stats(stats: dict) -> str:
    if stats.get("error"):
        return f"url-fill: {stats['error']}"
    filled, skipped = stats.get("filled", 0), stats.get("skipped", 0)
    if not filled and not skipped:
        return "url-fill: nothing to fill"
    line = f"url-fill: {filled} listing(s) given a search link"
    if skipped:
        line += f" ({skipped} skipped: unaddressed or excluded by policy)"
    return line
