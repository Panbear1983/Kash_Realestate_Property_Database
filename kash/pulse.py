"""The weekly market pulse — what the census actually saw, summarized for a phone.

The RentCast census photographs the whole filtered Staten Island market every two days and
the changelog records every difference it finds. Nobody was reading that record. This turns
one week of it into a short push: how much is on the market, what arrived, what got cheaper
(the buyer's top signal), what left, and what sits just above the alert cap waiting to drop
into range.

Everything is computed locally from pool.db — no API call, no model, no cost.
"""
from __future__ import annotations

import statistics
from datetime import date, timedelta


def _median(values):
    values = [v for v in values if v]
    return int(statistics.median(values)) if values else None


def compute(store, prefs: dict, days: int = 7, today: str | None = None) -> dict:
    today = today or date.today().isoformat()
    cutoff = (date.fromisoformat(today) - timedelta(days=days)).isoformat()

    events = store.conn.execute(
        "SELECT event, detail, match_key, ts FROM changelog WHERE ts > ? AND ts <= ?",
        (cutoff, today)).fetchall()

    new = [e for e in events if e["event"] == "new_listing"]
    drops = [e for e in events if e["event"] == "price_drop"]
    increases = [e for e in events if e["event"] == "price_increase"]
    gone = [e for e in events if e["event"] == "status_change"
            and any(s in (e["detail"] or "") for s in ("-> off_market", "-> pending", "-> sold"))]
    back = [e for e in events if e["event"] == "status_change" and "-> active" in (e["detail"] or "")]

    # Biggest cuts, with addresses, sorted by dollars saved.
    cut_lines = []
    for e in drops:
        try:
            old, newp = [int(p.strip()) for p in (e["detail"] or "").split("->")]
        except (ValueError, AttributeError):
            continue
        row = store.get(e["match_key"]) or {}
        cut_lines.append((old - newp, f"{row.get('street_address') or e['match_key']}: "
                                      f"${old:,} → ${newp:,} (−${old - newp:,})"))
    cut_lines.sort(reverse=True)

    rows = [r for r in store.all() if r.get("status") == "active"]
    price_lo = (prefs.get("price") or {}).get("min") or 0
    alert_cap = (prefs.get("eligibility") or {}).get("telegram_max_price")
    price_hi = (prefs.get("price") or {}).get("max")
    in_band = [r for r in rows
               if r.get("list_price") and price_lo <= r["list_price"] <= (price_hi or 10**9)]
    stretch = [r for r in in_band
               if alert_cap and r["list_price"] > alert_cap]

    return {
        "days": days,
        "active": len(rows),
        "in_band": len(in_band),
        "median_price": _median([r.get("list_price") for r in in_band]),
        "median_psf": _median([r.get("price_per_sqft") for r in in_band]),
        "new": len(new),
        "drops": len(drops),
        "increases": len(increases),
        "gone": len(gone),
        "back": len(back),
        "top_cuts": [line for _, line in cut_lines[:3]],
        "stretch_count": len(stretch),
        "alert_cap": alert_cap,
    }


def format_pulse(p: dict) -> str:
    lines = [f"📈 Kash market pulse — last {p['days']} days",
             f"{p['in_band']} homes on the market in your range"
             + (f" (median ${p['median_price']:,}" if p["median_price"] else "")
             + (f", ${p['median_psf']:,}/sqft)" if p["median_psf"] else ")" if p["median_price"] else "")]
    moves = [f"{p['new']} new"]
    if p["drops"]:
        moves.append(f"{p['drops']} price cut(s)")
    if p["increases"]:
        moves.append(f"{p['increases']} price increase(s)")
    if p["gone"]:
        moves.append(f"{p['gone']} left the market")
    if p["back"]:
        moves.append(f"{p['back']} came back")
    lines.append(" · ".join(moves))
    if p["top_cuts"]:
        lines.append("Biggest cuts:")
        lines += [f"  {c}" for c in p["top_cuts"]]
    if p["stretch_count"] and p["alert_cap"]:
        lines.append(f"Watchlist: {p['stretch_count']} home(s) sit above your "
                     f"${p['alert_cap']:,} alert cap — a cut drops them into range.")
    if not (p["new"] or p["drops"] or p["gone"]):
        lines.append("A quiet week.")
    return "\n".join(lines)
