"""Entity resolution — decide when two records are the same property.

Preference order for the match key:
  1. MLS number (authoritative, cross-source).
  2. Normalized street address + ZIP.
"""
from __future__ import annotations

import re
from typing import Optional

# Common USPS-style suffix normalizations so "Ave"/"Avenue"/"Av." collapse.
_SUFFIX = {
    "street": "st", "st": "st",
    "avenue": "ave", "ave": "ave", "av": "ave",
    "road": "rd", "rd": "rd",
    "lane": "ln", "ln": "ln",
    "drive": "dr", "dr": "dr",
    "court": "ct", "ct": "ct",
    "place": "pl", "pl": "pl",
    "boulevard": "blvd", "blvd": "blvd",
    "loop": "loop", "terrace": "ter", "ter": "ter",
    "green": "grn", "grn": "grn",
    "circle": "cir", "cir": "cir",
    "way": "way", "row": "row",
}


def normalize_address(street: Optional[str], zip_code: Optional[str]) -> Optional[str]:
    """Canonicalize a street + ZIP into a stable, comparable key.

    '45 Fairlawn Loop' / '10308' -> '45 fairlawn loop|10308'
    '90 Elson St.'      / '10308' -> '90 elson st|10308'
    """
    if not street:
        return None
    s = street.lower()
    s = re.sub(r"^\s*\[[^\]]*\]\s*", "", s)          # drop a leading [2-FAM] tag
    s = re.sub(r"[#,.]", " ", s)                      # punctuation -> space
    s = re.sub(r"\b(apt|unit|ste|suite|fl|floor)\b.*$", "", s)  # drop unit tails
    toks = s.split()
    toks = [_SUFFIX.get(t, t) for t in toks]
    s = " ".join(toks).strip()
    if not s:
        return None
    z = (zip_code or "").strip()
    return f"{s}|{z}"


_SITES = ("zillow", "realtor", "redfin", "trulia", "compass", "homes")


def site_from_url(url: Optional[str]) -> Optional[str]:
    """Map a listing URL to its source-site label, e.g. www.zillow.com/... -> 'zillow'."""
    if not url:
        return None
    m = re.search(r"https?://(?:www\.)?([^/]+)", url)
    host = m.group(1).lower() if m else ""
    for s in _SITES:
        if s in host:
            return s
    return host.split(".")[0] or None


def match_key(record: dict) -> Optional[str]:
    """Return the dedup key for a record. Normalized address+ZIP is the PRIMARY key: it's
    always present and stable across a row's lifetime. (MLS number can arrive later via the
    detail-scrape enrichment — keying on it would change the row's key mid-life and orphan
    subsequent updates.) MLS is only a fallback when there is no usable address."""
    addr = normalize_address(record.get("street_address"), record.get("zip"))
    if addr:
        return f"addr:{addr}"
    mls = record.get("mls_number")
    return f"mls:{str(mls).strip().lower()}" if mls else None
