"""Codex auto-ranking of new scraped listings.

New listings arrive with raw facts but no tier/priority/thesis (those were hand-curated in
the seed). For each new, unranked row (`tier IS NULL AND source != 'seed:docx'`) we ask
Codex — given the buyer's preferences and a few curated examples — to assign a tier,
view_priority, brrrr_rating, target_buy_price and a one-line analysis, so the new find is
comparable and sortable next to the curated set. Auto rows are marked `[auto]` in analysis.

Runs once per listing (skips rows that already have a tier) and is bounded per run, so
Codex cost stays small. Never touches seed/human rows (their tier/analysis are non-null and
also USER_PROTECTED on merge).
"""
from __future__ import annotations

import json
from typing import Optional

from .dedup import match_key
from .llm import route
from .signals import multigenerational

RANK_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "tier": {"type": "string", "enum": ["S", "A", "B", "C", "W", "X"]},
        "view_priority": {"type": "string",
                          "enum": ["now", "soon", "worth", "call", "watch", "skip"]},
        "brrrr_rating": {"type": "integer"},
        "target_buy_price": {"type": "integer"},
        "arv_estimate": {"type": "integer"},
        "appreciation_pct": {"type": "number"},
        "analysis": {"type": "string"},
    },
    "required": ["tier", "view_priority", "brrrr_rating", "target_buy_price",
                 "arv_estimate", "appreciation_pct", "analysis"],
}

_FACTS = ["neighborhood", "zip", "property_type", "list_price", "beds", "baths", "sqft",
          "price_per_sqft", "year_built", "zestimate", "school_gs_rating", "flood_zone",
          "estimated_rent_monthly", "days_on_market"]


_SIGNALS = ["signal_multigenerational", "signal_separate_entrance", "signal_second_kitchen",
            "signal_condition", "signal_friction"]


def _prompt(listing: dict, prefs: dict, exemplars: list[dict]) -> str:
    facts = {k: listing.get(k) for k in _FACTS if listing.get(k) is not None}
    # Description signals (kash/enrich/describe.py) — the numeric facts alone say nothing about
    # multigenerational capability or renovation burden, which is most of the thesis.
    signals = {k[len("signal_"):]: listing.get(k) for k in _SIGNALS if listing.get(k) is not None}
    ex = "\n".join(
        f"- {e.get('street_address')} ({e.get('neighborhood')}, ${e.get('list_price')}, "
        f"{e.get('beds')}bd/{e.get('baths')}ba, GS {e.get('school_gs_rating')}, "
        f"flood {e.get('flood_zone')}) -> tier {e.get('tier')}: "
        f"{(e.get('analysis') or '')[:110]}"
        for e in exemplars
    )
    return (
        "Rank a Staten Island home for a buyer/investor, matching the style of their "
        "hand-curated shortlist. Assign: tier (S best … X skip), view_priority "
        "(now/soon/worth/call/watch/skip), brrrr_rating (1-5), target_buy_price (integer, "
        "a bit under list), arv_estimate (after-repair/market value, integer), "
        "appreciation_pct (expected annual appreciation, e.g. 8), and a one-sentence "
        "analysis. Output ONLY JSON.\n"
        f"Buyer preferences: price band {prefs.get('price')}, target neighborhoods "
        f"{', '.join(prefs.get('neighborhoods', [])[:8])}, beds_min {prefs.get('beds_min')}; "
        "favors strong school ratings (GS) and low flood risk (flood_zone X), penalizes "
        "AE/VE flood zones.\n"
        f"Examples from their curated list:\n{ex}\n\n"
        f"Listing to rank: {json.dumps(facts)}\n"
        + (f"Description signals: {json.dumps(signals)}" if signals else
           "Description signals: none extracted yet.")
    )


def _record(store, backend):
    """Bill one batch call to the system actor. Never raises."""
    try:
        from .usage import SYSTEM, Usage
        if getattr(backend, "chosen", None):
            Usage(store).record(SYSTEM, backend.chosen, job="rank",
                                usage=getattr(backend, "last_usage", None))
    except Exception:  # noqa: BLE001
        pass


def apply_property_priority(updates: dict, listing: dict) -> dict:
    """Apply non-negotiable buyer preferences after model ranking."""
    updates = dict(updates)
    is_multigen, why = multigenerational(listing)
    if is_multigen:
        updates["view_priority"] = "now"
        updates["analysis"] = f"TOP PRIORITY: {why}. " + (updates.get("analysis") or "")
    return updates


def rank_new(store, prefs: dict, limit: Optional[int] = 15, backend=None) -> dict:
    # 'rank' ladder: a nightly judgment call, so depth beats latency here. Batch work bills to
    # the 'system' actor so it can't eat a person's daily allowance.
    from .usage import SYSTEM
    backend = backend or route(prefs.get("llm"), job="rank", actor=SYSTEM, store=store)
    ok, why = backend.available()
    if not ok:
        return {"ranked": 0, "skipped": why}

    rows = store.all()
    # the hand-curated originals (identified by property_id) are the ranking exemplars
    exemplars = [r for r in rows if r.get("property_id") and r.get("tier")][:3]
    new = [r for r in rows if not r.get("tier")]
    if limit:
        new = new[:limit]

    ranked = 0
    for r in new:
        key = match_key(r)
        if not key:
            continue
        try:
            spec = backend.query_spec(_prompt(r, prefs, exemplars), RANK_SCHEMA)
        except Exception:  # noqa: BLE001 — one bad rank shouldn't stop the batch
            continue
        _record(store, backend)
        updates = {
            "tier": spec.get("tier"),
            "view_priority": spec.get("view_priority"),
            "brrrr_rating": spec.get("brrrr_rating"),
            "target_buy_price": spec.get("target_buy_price"),
            "arv_estimate": spec.get("arv_estimate"),
            "appreciation_pct": spec.get("appreciation_pct"),
            "analysis": "[auto] " + (spec.get("analysis") or ""),
        }
        updates = apply_property_priority(updates, r)
        # tier and analysis are USER_PROTECTED; ranking is the one writer allowed to set them,
        # and only on rows that have none yet (rank_new filters on tier IS NULL).
        store.update_fields(key, {k: v for k, v in updates.items() if v is not None},
                            allow_protected=True)
        ranked += 1
    return {"ranked": ranked, "candidates": len(new)}
