"""FEMA flood zone for a coordinate — free, no API key, with a provider chain.

(latitude, longitude) -> (zone, status). Staten Island has real coastal exposure (Midland
Beach, New Dorp Beach, Oakwood Beach), so this decides genuine risk per property — and
`notifications.alert_ready` will not push a listing whose zone is not in `safe_flood_zones`.

Two problems this module exists to avoid:

1. **A failed lookup must never read as safe.** The previous version returned "X" — the safe
   zone — whenever a query came back with no features. That conflated "outside a mapped hazard
   area" with "the query returned nothing for some other reason": wrong service URL, point
   outside the layer's extent, silent API change. A false "X" marks a flood-exposed home as
   verified-safe and pushes it. Status is now explicit and callers write nothing on
   `unavailable`.

2. **"No features" is only meaningful if the layer covers the area.** Both providers here hold
   only special flood hazard areas, so an empty result is the normal way of saying "zone X" —
   but that inference is worthless if the service is degraded or its extent excludes Staten
   Island. So each provider is coverage-probed against a reference point with a known AE zone
   before an empty result is believed. Probe failure downgrades the answer to `unavailable`
   rather than fabricating safety.

At the time of writing every `fema.gov` host is unreachable from this machine — both
`hazards.fema.gov` and `msc.fema.gov` die in the TLS handshake (`SSLEOFError`), in curl as well
as Python, while other HTTPS hosts respond normally. FEMA is kept first in the chain because
that may be transient; the Esri-hosted mirror is what actually answers today.
"""
from __future__ import annotations

import time
from typing import Optional

import requests

RESOLVED = "resolved"        # a zone was found
NOT_MAPPED = "not_mapped"    # confirmed outside any mapped hazard area -> zone X
UNAVAILABLE = "unavailable"  # could not find out; callers must not write a zone

SAFE_DEFAULT = "X"

# A point with a known, stable AE zone. If a provider cannot return AE here, it is not
# usable for Staten Island and its empty results mean nothing.
PROBE_POINT = (40.5717, -74.0907)   # Midland Beach
PROBE_EXPECT = "AE"
_PROBE_TTL = 900

_probe_cache: dict[str, tuple[bool, float]] = {}


class _ArcGISProvider:
    """Esri-hosted FEMA NFHL derivative. Reachable when fema.gov is not."""

    name = "arcgis_nfhl"
    URL = ("https://services.arcgis.com/P3ePLMYs2RVChkJx/ArcGIS/rest/services/"
           "USA_Flood_Hazard_Reduced_Set_gdb/FeatureServer/0/query")

    def query(self, lat: float, lon: float, timeout: int = 30) -> Optional[str]:
        """Zone string, or None for 'no polygon here'. Raises on transport failure."""
        r = requests.get(self.URL, params={
            "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint",
            "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
            "outFields": "FLD_ZONE,SFHA_TF", "returnGeometry": "false", "f": "json",
        }, timeout=timeout)
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"{self.name}: {body['error'].get('message')}")
        feats = body.get("features") or []
        if not feats:
            return None
        return feats[0].get("attributes", {}).get("FLD_ZONE") or None


class _FemaProvider:
    """The original FEMA NFHL service. Kept first — its outage may be temporary."""

    name = "fema_nfhl"
    URL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"

    def query(self, lat: float, lon: float, timeout: int = 30) -> Optional[str]:
        r = requests.get(self.URL, params={
            "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint",
            "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
            "outFields": "FLD_ZONE,SFHA_TF", "returnGeometry": "false", "f": "json",
        }, timeout=timeout)
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"{self.name}: {body['error'].get('message')}")
        feats = body.get("features") or []
        if not feats:
            return None
        return feats[0].get("attributes", {}).get("FLD_ZONE") or None


PROVIDERS = [_FemaProvider(), _ArcGISProvider()]


def _covers_staten_island(provider, timeout: int = 30) -> bool:
    """Does this provider return the expected zone at the reference point?"""
    hit = _probe_cache.get(provider.name)
    now = time.monotonic()
    if hit and (now - hit[1]) < _PROBE_TTL:
        return hit[0]
    try:
        ok = provider.query(*PROBE_POINT, timeout=timeout) == PROBE_EXPECT
    except Exception:  # noqa: BLE001 — unreachable counts as no coverage
        ok = False
    _probe_cache[provider.name] = (ok, now)
    return ok


def reset_probe_cache() -> None:
    _probe_cache.clear()


def flood_zone_status(lat: float, lon: float, timeout: int = 30) -> tuple[Optional[str], str]:
    """Return (zone, status). Never returns a zone alongside UNAVAILABLE."""
    if lat is None or lon is None:
        return None, UNAVAILABLE
    problems = []
    for provider in PROVIDERS:
        if not _covers_staten_island(provider, timeout=timeout):
            problems.append(f"{provider.name}: no coverage")
            continue
        try:
            zone = provider.query(lat, lon, timeout=timeout)
        except Exception as e:  # noqa: BLE001 — try the next provider
            problems.append(f"{provider.name}: {type(e).__name__}")
            continue
        if zone:
            return zone, RESOLVED
        # Empty, from a provider we just confirmed covers the area: genuinely unmapped.
        return SAFE_DEFAULT, NOT_MAPPED
    return None, UNAVAILABLE


def flood_zone(lat: float, lon: float, timeout: int = 30) -> Optional[str]:
    """Zone only, or None when it could not be determined.

    Kept for callers that don't need the status. Note the deliberate difference from the old
    behaviour: an unavailable lookup returns None, not "X".
    """
    zone, status = flood_zone_status(lat, lon, timeout=timeout)
    return zone if status in (RESOLVED, NOT_MAPPED) else None
