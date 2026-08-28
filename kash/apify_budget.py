"""How much of the Apify monthly credit is gone — printed where the owner already looks.

The free tier is a HARD $5.00/month: past it, actor runs fail rather than bill. The nightly
search/detail budget was designed to that ceiling, so drift needs to be visible before it
becomes failed runs. One free GET per cycle; anything unexpected — no token, network error,
schema change, zero cap — degrades to None and the run report simply omits the line.
"""
from __future__ import annotations

import os
from typing import Optional

LIMITS_URL = "https://api.apify.com/v2/users/me/limits"

# health.assess flags the run once usage crosses this share of the cap. 0.85 rather than
# 0.9: at nightly burn the 90% line left no run in which throttling could still help.
WARN_AT = 0.85


def month_to_date(token: Optional[str] = None, request_get=None) -> Optional[dict]:
    token = token or os.environ.get("APIFY_TOKEN")
    if not token:
        return None
    if request_get is None:
        import requests
        request_get = requests.get
    try:
        resp = request_get(LIMITS_URL, params={"token": token}, timeout=10)
        payload = resp.json() or {}
        data = payload.get("data") or payload
        used = float(data["current"]["monthlyUsageUsd"])
        cap = float(data["limits"]["maxMonthlyUsageUsd"])
        if cap <= 0:
            return None
        cycle = data.get("monthlyUsageCycle") or {}
        return {"used": used, "cap": cap, "pct": used / cap,
                "cycle_start": cycle.get("startAt"), "cycle_end": cycle.get("endAt")}
    except Exception:  # noqa: BLE001 — observability must never take the run down
        return None


def format_line(budget: dict) -> str:
    return (f"apify: ${budget['used']:.2f} of ${budget['cap']:.2f} "
            f"used this cycle ({budget['pct'] * 100:.0f}%)")
