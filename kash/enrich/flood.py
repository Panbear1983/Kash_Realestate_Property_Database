"""FEMA flood zone via the National Flood Hazard Layer (NFHL) — free, no API key.

(latitude, longitude) -> FEMA flood zone code (e.g. 'X', 'AE', 'VE'). Staten Island has
significant coastal flood exposure (Midland Beach, etc.), so this flags real risk per
property. No polygon at the point means the area is outside mapped hazard zones -> 'X'.
"""
from __future__ import annotations

from typing import Optional

import requests

NFHL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"


def flood_zone(lat: float, lon: float) -> Optional[str]:
    r = requests.get(
        NFHL,
        params={
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "FLD_ZONE,SFHA_TF",
            "returnGeometry": "false",
            "f": "json",
        },
        timeout=30,
    )
    r.raise_for_status()
    feats = r.json().get("features", [])
    if not feats:
        return "X"  # outside mapped hazard zones = minimal risk
    return feats[0]["attributes"].get("FLD_ZONE") or "X"
