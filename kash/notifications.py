"""Pure Telegram recipient and message policy; sending remains in run_update.py."""
from __future__ import annotations

from urllib.parse import urlparse

from .dedup import match_key
from .eligibility import alert_ready
from .signals import score_description


def _is_clickable_url(value) -> bool:
    parsed = urlparse(str(value or ""))
    return parsed.scheme == "https" and bool(parsed.netloc)


def testing_recipients(allowed_ids: list[int], prefs: dict) -> list[int]:
    """Use explicit testing recipients when configured, while honoring access control."""
    configured = prefs.get("telegram_recipient_ids")
    if not configured:
        return allowed_ids
    allowed = set(allowed_ids)
    return [int(user_id) for user_id in configured if int(user_id) in allowed]


def actionable_listings(rows: list[dict], prefs: dict) -> list[dict]:
    """Keep only active, alert-safe listings with a link Telegram can open."""
    return [
        row for row in rows
        if row.get("status") == "active" and _is_clickable_url(row.get("listing_url")) and alert_ready(row, prefs)
    ]


def listing_delivery_kind(row: dict) -> str | None:
    key = match_key(row)
    return f"listing:{key}" if key else None


def unsent_actionable_listings(store, recipient_id: int, prefs: dict) -> list[dict]:
    """Find alert-ready rows that this recipient has not already received."""
    rows = actionable_listings(store.all(), prefs)
    return [
        row for row in rows
        if (kind := listing_delivery_kind(row)) and not store.notification_sent(recipient_id, kind)
    ]


def format_onboarding() -> str:
    return (
        "Robo Kash testing is active. Robo Kash stores qualifying homes for review and sends "
        "linked alerts only for 2.5+ bath homes with a verified safe flood status. "
        "Telegram is read-only for now; search settings stay in the local dashboard. "
        "two-way Telegram interaction is planned for a later controlled update."
    )


def _price_text(value) -> str:
    return f"${value:,.0f}" if isinstance(value, (int, float)) else "price unavailable"


def format_listing_brief(row: dict) -> str:
    """Render a short, factual, lightly playful listing note without an LLM."""
    facts = []
    if row.get("beds") is not None:
        facts.append(f"{row['beds']} bd")
    if row.get("baths") is not None:
        facts.append(f"{row['baths']} ba")
    details = " · ".join(facts) or "home details pending"
    signals = score_description(row.get("listing_description"))
    highlights = []
    if "separate entrance" in signals.reasons:
        highlights.append("separate entrance — golden-ticket setup")
    if "second kitchen" in signals.reasons:
        highlights.append("second kitchen in the listing notes")
    if "in-law suite" in signals.reasons:
        highlights.append("in-law suite noted")
    if not highlights:
        highlights.append("Robo Kash has the basics on deck; no extra feature claims, no bullshit")
    return (
        f"🤖 Robo Kash: {row.get('street_address') or 'Listing'} — {_price_text(row.get('list_price'))}\n"
        f"{details}\n"
        f"Quick take: {'; '.join(highlights)}.\n"
        f"🔗 {row['listing_url']}"
    )


def format_testing_digest(rows: list[dict]) -> str:
    if not rows:
        return "Robo Kash testing update: no new actionable listings today."
    lines = ["Robo Kash testing: actionable listings"]
    for row in rows[:20]:
        lines.append(format_listing_brief(row))
    return "\n".join(lines)
