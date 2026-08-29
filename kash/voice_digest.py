"""The morning report, rewritten to be heard instead of read.

The written digest cannot simply be spoken. It is a list of bare numbers and internal
match keys — `875000 -> 850000  (addr:35 shawnee st|10301)` — which a speech engine
renders as "eight hundred seventy five thousand minus eight hundred fifty thousand,
address thirty five shawnee ess tee, pipe, one zero three zero one". Unusable.

So this builds a separate spoken script from the same run: how much changed, the single
most useful number, and the one listing worth waking up for. The written messages that
precede the voice note stay the complete record, links and all; the audio only has to be
worth the forty seconds it takes to hear.

Pure functions — no I/O, no network. run_update.py owns sending, as with every other
Telegram push in this project.
"""
from __future__ import annotations

import re
from datetime import date

# Detail formats written by kash/pipeline.py and kash/lifecycle.py into the changelog.
# Parsed rather than re-derived because the changelog is the only record of what changed
# in *this* run; every parse degrades to "skip this detail", never to a wrong number.
_NEW = re.compile(r"^(?P<addr>.+?)\s*@\s*(?P<price>[\d,]+)\s*$")
_DROP = re.compile(r"^\s*(?P<old>\d[\d,]*)\s*->\s*(?P<new>\d[\d,]*)")
_ADDR_KEY = re.compile(r"^addr:([^|]+)")
_GONE_MARKERS = ("-> pending", "-> off_market", "-> sold")

# Spoken out, "st" is heard as "Saint" and "Rd" is often spelled letter by letter.
_STREET_WORDS = {
    "ave": "Avenue", "av": "Avenue", "st": "Street", "str": "Street", "rd": "Road",
    "dr": "Drive", "ln": "Lane", "blvd": "Boulevard", "ct": "Court", "pl": "Place",
    "ter": "Terrace", "terr": "Terrace", "pkwy": "Parkway", "hwy": "Highway",
    "cir": "Circle", "sq": "Square", "n": "North", "s": "South", "e": "East", "w": "West",
}


def _spoken_address(text: str) -> str:
    """Expand an address's abbreviations and restore its capitalization for speech."""
    out = []
    for word in re.split(r"\s+", str(text or "").strip()):
        bare = word.strip(".,").lower()
        if bare in _STREET_WORDS:
            out.append(_STREET_WORDS[bare])
        elif word.islower():
            out.append(word.capitalize())
        elif word:
            out.append(word)
    return " ".join(out)


def _address_from_key(match_key) -> str:
    m = _ADDR_KEY.match(str(match_key or ""))
    return _spoken_address(m.group(1)) if m else ""


def _price(value) -> str:
    """Money as digits with separators — both speech engines verbalize that correctly."""
    try:
        return f"${float(str(value).replace(',', '').replace('$', '')):,.0f}"
    except (TypeError, ValueError):
        return ""


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


def summarize(events) -> dict:
    """Bucket one run's changelog events into what a person would actually report.

    Mirrors kash/digest.py's grouping so the spoken and written versions can never
    disagree about what happened; it keeps the parsed numbers that the written one
    discards, because the spoken version needs to name them.
    """
    out: dict[str, list[dict]] = {"new": [], "price_drop": [], "gone": [], "back": []}
    for event in events or []:
        kind = event.get("event")
        detail = str(event.get("detail") or "")
        key = event.get("match_key")
        if kind == "new_listing":
            m = _NEW.match(detail)
            out["new"].append({
                "address": _spoken_address(m.group("addr")) if m else _address_from_key(key),
                "price": _price(m.group("price")) if m else "",
            })
        elif kind == "price_drop":
            m = _DROP.match(detail)
            if not m:
                continue
            old, new = (int(m.group(g).replace(",", "")) for g in ("old", "new"))
            if new >= old:          # not actually a drop; say nothing rather than something wrong
                continue
            out["price_drop"].append({
                "address": _address_from_key(key), "old": old, "new": new, "cut": old - new,
            })
        elif kind == "status_change":
            if any(marker in detail for marker in _GONE_MARKERS):
                out["gone"].append({"address": _address_from_key(key)})
            elif "-> active" in detail:
                out["back"].append({"address": _address_from_key(key)})
    return out


def _counts_sentence(groups: dict) -> str:
    parts = []
    if groups["new"]:
        parts.append(_plural(len(groups["new"]), "new listing", "new listings"))
    if groups["price_drop"]:
        parts.append(_plural(len(groups["price_drop"]), "price drop", "price drops"))
    if groups["gone"]:
        parts.append(_plural(len(groups["gone"]), "home", "homes") + " off the market")
    if groups["back"]:
        parts.append(_plural(len(groups["back"]), "home", "homes") + " back on")
    return f"{_join(parts).capitalize()}." if parts else ""


def _biggest_drop_sentence(groups: dict) -> str:
    """The single most actionable number in the run — the deepest cut, named."""
    drops = groups["price_drop"]
    if not drops:
        return ""
    best = max(drops, key=lambda d: d["cut"])
    where = f"{best['address']} is" if best["address"] else "One home is"
    return f"The biggest cut: {where} down from {_price(best['old'])} to {_price(best['new'])}."


def _top_listing_sentence(listings) -> str:
    """The first of the recipient's alert-ready rows — already sorted best-first by
    kash/notifications.py, so this is the home the run itself ranks highest."""
    rows = list(listings or [])
    if not rows:
        return ""
    row = rows[0]
    # `street_address` is the pool's column name — the same one format_listing_brief reads.
    address = _spoken_address(row.get("street_address") or row.get("address") or "")
    price = _price(row.get("list_price"))
    if not address:
        return ""
    lead = "Top of your list" if len(rows) == 1 else f"Top of the {len(rows)} on your list"
    return f"{lead}: {address}{f' at {price}' if price else ''}."


def spoken_briefing(events, listings=None, *, today: date | None = None) -> str:
    """The script for the morning voice note. Empty string when there is nothing to say."""
    groups = summarize(events)
    body = [s for s in (_counts_sentence(groups),
                        _biggest_drop_sentence(groups),
                        _top_listing_sentence(listings)) if s]
    if not body:
        return ""
    weekday = (today or date.today()).strftime("%A")
    opening = f"Good morning. Robo Kash here with your Staten Island update for {weekday}."
    closing = "The full list, with the links, is in the message above."
    return " ".join([opening, *body, closing])
