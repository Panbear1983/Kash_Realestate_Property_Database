"""Geocoding via the US Census Geocoder — free, no API key.

address -> (latitude, longitude). Used to populate coordinates so downstream enrichers
(flood zone, and later commute/schools) have a point to work from.
"""
from __future__ import annotations

from typing import Optional

import requests

CENSUS = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"


def geocode(street: str, zip_code: Optional[str],
            city: str = "Staten Island", state: str = "NY") -> Optional[dict]:
    if not street:
        return None
    addr = f"{street}, {city}, {state} {zip_code or ''}".strip()
    r = requests.get(
        CENSUS,
        params={"address": addr, "benchmark": "Public_AR_Current", "format": "json"},
        timeout=30,
    )
    r.raise_for_status()
    matches = r.json().get("result", {}).get("addressMatches", [])
    if not matches:
        return None
    c = matches[0]["coordinates"]
    return {"latitude": round(c["y"], 6), "longitude": round(c["x"], 6)}
