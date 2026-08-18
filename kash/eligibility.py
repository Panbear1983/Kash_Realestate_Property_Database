"""Pure listing-admission and Telegram-qualification policy."""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Decision:
    admit: bool
    telegram_eligible: bool
    requires_flood_verification: bool
    reasons: tuple[str, ...]


def bath_count(value) -> float | None:
    """Return the first numeric bath count from provider text, if available."""
    if value is None:
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(match.group()) if match else None


def classify(listing: dict, prefs: dict) -> Decision:
    cfg = prefs.get("eligibility") or {}
    excluded = {str(v).lower() for v in cfg.get("excluded_property_types", ())}
    property_type = str(listing.get("property_type") or "").lower()
    if property_type in excluded:
        return Decision(False, False, False, ("excluded_property_type",))

    baths = bath_count(listing.get("baths"))
    if baths is None:
        return Decision(True, False, False, ("unparseable_bath_count",))

    storage_minimum = float(cfg.get("store_min_baths", 2))
    if baths < storage_minimum:
        return Decision(False, False, False, ("bath_count_below_storage_minimum",))

    minimum = float(cfg.get("telegram_min_baths", 2))
    if baths < minimum:
        return Decision(True, False, False, ("bath_count_below_alert_minimum",))

    maximum_price = cfg.get("telegram_max_price")
    if maximum_price is not None:
        try:
            price = float(listing.get("list_price"))
            maximum_price = float(maximum_price)
        except (TypeError, ValueError):
            return Decision(True, False, False, ("unparseable_list_price",))
        if price > maximum_price:
            return Decision(True, False, False, ("list_price_above_alert_maximum",))

    return Decision(True, True, not bool(listing.get("flood_zone")), ())


def alert_ready(listing: dict, prefs: dict) -> bool:
    """Return whether a stored listing may enter the automatic alert queue."""
    decision = classify(listing, prefs)
    flood_zone = str(listing.get("flood_zone") or "").upper()
    safe_zones = {str(zone).upper() for zone in (prefs.get("eligibility") or {}).get("safe_flood_zones", ["X"])}
    return decision.telegram_eligible and flood_zone in safe_zones
