"""Change digest — turns a run's changelog events into the human summary your doc keeps
by hand ('MAJOR LOSSES', 'KEY PRICE DROPS', 'KEY NEW ADDS'). Consumed by the Telegram
push (Phase 3 wiring) and printed by run_update.py.
"""
from __future__ import annotations


def build(events: list[dict]) -> dict:
    groups = {"new": [], "price_drop": [], "gone": [], "back": []}
    for e in events:
        ev, detail, key = e["event"], e["detail"], e["match_key"]
        if ev == "new_listing":
            groups["new"].append(detail)
        elif ev == "price_drop":
            groups["price_drop"].append(f"{detail}  ({key})")
        elif ev == "status_change":
            if any(s in detail for s in ("-> pending", "-> off_market", "-> sold")):
                groups["gone"].append(f"{detail}  ({key})")
            elif "-> active" in detail:
                groups["back"].append(f"{detail}  ({key})")
    return groups


def format_text(groups: dict) -> str:
    if not any(groups.values()):
        return "No changes this run."
    order = [
        ("gone", "MAJOR LOSSES (pending / off-market / sold)"),
        ("price_drop", "PRICE DROPS"),
        ("new", "NEW LISTINGS"),
        ("back", "BACK ON MARKET"),
    ]
    lines = []
    for key, title in order:
        items = groups.get(key) or []
        if items:
            lines.append(f"{title} ({len(items)})")
            lines.extend(f"  - {it}" for it in items[:30])
    return "\n".join(lines)
