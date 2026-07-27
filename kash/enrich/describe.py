"""Semantic extraction from the listing description.

`kash/signals.py` approximates this with substring matching — it looks for the literal
strings "separate entrance", "second kitchen", "in-law suite". That decides a lot: a hit
makes `rank.apply_property_priority` force `view_priority="now"` and prefix TOP PRIORITY,
which is what drives the Telegram push. So a listing worded "private side entry to a
finished basement with kitchenette" — a textbook mother/daughter setup, and three of the six
configured property_types are 2-family — currently scores nothing.

This reads the same description with a model instead, against a fixed schema, and stores the
result in machine-owned `signal_*` columns. `signal_evidence` quotes the source text so every
flag can be checked against what the listing actually said.

Bounded per run and idempotent: a row is extracted once (tracked by `signal_extracted_at`)
and skipped thereafter, so re-running the cycle costs nothing.
"""
from __future__ import annotations

import json
from datetime import date

from ..dedup import match_key

SIGNAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "multigenerational": {"type": "boolean"},
        "separate_entrance": {"type": "boolean"},
        "second_kitchen": {"type": "boolean"},
        "condition": {"type": "string", "enum": ["turnkey", "cosmetic", "gut"]},
        "friction": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "string"},
    },
    "required": ["multigenerational", "separate_entrance", "second_kitchen",
                 "condition", "friction", "evidence"],
}


def _prompt(description: str) -> str:
    return (
        "Read this property listing description and extract structured signals for a "
        "Staten Island buyer who wants multigenerational capability (a self-contained second "
        "living area) and wants to know the renovation burden. Output ONLY JSON.\n"
        "- multigenerational: true if the home could house a second household semi-independently "
        "— a separate/private entrance, in-law or au-pair suite, second kitchen or kitchenette, "
        "mother/daughter layout, finished basement with its own entry, or dual utilities. "
        "Judge the arrangement described, not the words used.\n"
        "- separate_entrance / second_kitchen: true only if the text actually indicates one.\n"
        "- condition: 'turnkey' (move-in ready, recently updated), 'cosmetic' (dated but "
        "livable, needs paint/floors/kitchen), or 'gut' (as-is, needs everything, "
        "structural/systems work).\n"
        "- friction: short phrases for anything that would put a buyer off — busy road, "
        "commercial adjacency, flood history, tenant in place, estate/as-is sale, no parking. "
        "Empty list if none.\n"
        "- evidence: the single most relevant phrase quoted verbatim from the description "
        "(empty string if nothing supports any flag).\n"
        "Do not infer from the absence of information — if the description doesn't say, it's "
        "false or empty.\n\n"
        f"Description:\n{description}"
    )


def _needs_extract(row: dict, prefs: Optional[dict] = None) -> bool:
    """Worth spending an LLM call on? Out-of-scope rows are not.

    The pool holds rows admitted before the ZIP allow-list existed. Extracting their
    descriptions burns subscription quota on homes the buyer's own filter rejects.
    """
    if not row.get("listing_description") or row.get("signal_extracted_at"):
        return False
    if prefs:
        from ..notifications import in_alert_scope
        return in_alert_scope(row, prefs)
    return True


def extract_descriptions(store, prefs: dict, limit: int = 15, backend=None) -> dict:
    """Fill signal_* columns for rows that have a description but no extraction yet."""
    from .. import llm

    rows = [r for r in store.all() if _needs_extract(r, prefs)]
    if not rows:
        return {"extracted": 0, "candidates": 0}

    from ..usage import SYSTEM, Usage
    backend = backend or llm.route((prefs or {}).get("llm"), job="extract",
                                   actor=SYSTEM, store=store)
    ok, why = backend.available()
    if not ok:
        return {"extracted": 0, "candidates": len(rows), "skipped": why}

    if limit:
        rows = rows[:limit]

    extracted, failed = 0, 0
    for row in rows:
        key = match_key(row)
        if not key:
            continue
        try:
            spec = backend.query_spec(_prompt(row["listing_description"]), SIGNAL_SCHEMA)
        except Exception:  # noqa: BLE001 — one bad row must not end the batch
            failed += 1
            continue
        try:
            if getattr(backend, "chosen", None):
                Usage(store).record(SYSTEM, backend.chosen, job="extract",
                                    usage=getattr(backend, "last_usage", None))
        except Exception:  # noqa: BLE001 — metering must not fail the batch
            pass
        friction = spec.get("friction") or []
        if isinstance(friction, str):          # tolerate a comma string from a loose backend
            friction = [f.strip() for f in friction.split(",") if f.strip()]
        store.update_fields(key, {
            "signal_multigenerational": bool(spec.get("multigenerational")),
            "signal_separate_entrance": bool(spec.get("separate_entrance")),
            "signal_second_kitchen": bool(spec.get("second_kitchen")),
            "signal_condition": spec.get("condition"),
            "signal_friction": friction,
            "signal_evidence": (spec.get("evidence") or "")[:500],
            "signal_extracted_at": date.today().isoformat(),
        })
        extracted += 1

    out = {"extracted": extracted, "candidates": len(rows)}
    if failed:
        out["failed"] = failed
    return out
