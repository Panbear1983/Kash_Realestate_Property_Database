"""Cached, read-only summary of the pool's enum-like columns — feeds the routing prompt
with real stored values instead of bare column names, and builds live example queries for
terminal messages. Never row content, never a private field.

The routing prompt is built by the shared prefix of kash.chat.handle() BEFORE the route is
known, and that prefix must never touch the database for general_reasoning/web_research/
clarify routes (proven by store=Bomb() fakes in tests/test_phase3_routing.py that raise on
any attribute access). So vocabulary can't come from a fresh per-message read at that
point. Instead it's refreshed opportunistically from kash.chat._database_reply (which
already touches the store unconditionally) and read elsewhere via the argument-free
peek_*() functions below, which are safe to call from anywhere — including code paths that
hold no store handle at all. Trade-off: the very first message in a fresh process sees the
old bare-column prompt; every message after any database-touching interaction sees
vocabulary.
"""
from __future__ import annotations

import time
from typing import Optional

from . import chat_policy, reasoning_contract

VOCAB_TTL_SECONDS = 600      # the pool changes at most a few times/day (nightly job); a
                              # 10-minute-stale enum list is never wrong, only briefly behind.
_ENUM_FIELDS = ("property_type", "status", "tier", "view_priority", "flood_zone")
_TOP_NEIGHBORHOODS = 15
_MAX_VALUES_PER_FIELD = 20   # guards a runaway/free-text column added to _ENUM_FIELDS later

_cache: dict = {"prompt_block": "", "examples": (), "at": None}


def _distinct(ro, field: str, limit: int) -> list[tuple[str, int]]:
    # field is always one of the hardcoded literals above — never user input — so this
    # f-string carries the same trust level as query.py's own validated interpolations.
    sql = (f'SELECT "{field}" AS v, COUNT(*) AS n FROM listings '
           f'WHERE "{field}" IS NOT NULL AND "{field}" != \'\' '
           f'GROUP BY "{field}" ORDER BY n DESC LIMIT ?')
    return [(r["v"], r["n"]) for r in ro.aggregate_select(sql, (limit,))]


def _safe_flood_zones(prefs: dict) -> list[str]:
    # Mirrors kash.eligibility.alert_ready's exact lookup so this can never drift from the
    # value that actually governs the buyer's alert queue.
    zones = (prefs.get("eligibility") or {}).get("safe_flood_zones", ["X"])
    return [str(z).upper() for z in zones] or ["X"]


def build(ro, prefs: dict) -> dict:
    """Compute the prompt vocabulary block and example queries fresh. Aggregate-only reads."""
    lines = []
    for field in _ENUM_FIELDS:
        values = _distinct(ro, field, _MAX_VALUES_PER_FIELD)
        if values:
            lines.append(f"{field}: " + ", ".join(str(v) for v, _ in values))
    hoods = _distinct(ro, "neighborhood", _TOP_NEIGHBORHOODS)
    if hoods:
        lines.append("neighborhood (top by listing count): " + ", ".join(v for v, _ in hoods))

    safe_zones = _safe_flood_zones(prefs)
    lines.append(
        f"flood_zone semantics: {'/'.join(safe_zones)} = this buyer's configured safe/"
        "minimal-risk FEMA zone(s) (preferences.eligibility.safe_flood_zones); any other "
        "value, or a missing flood_zone, means elevated or unverified risk."
    )
    lines.append(
        "Known values for filterable columns above must be used EXACTLY as stored — a "
        "user's words like 'house', 'condo', or 'a good flood zone' rarely match a stored "
        "value verbatim. Map to the closest listed value, or omit the filter if nothing "
        "plausible matches; do not invent a value that is not listed."
    )
    lines.append(
        'Example: "homes near a safe flood zone under $700k" -> filters '
        f'[{{"field":"flood_zone","op":"=","value":"{safe_zones[0]}"}}, '
        '{"field":"list_price","op":"<=","value":"700000"}].'
    )
    prompt_block = "\n".join(lines)

    summary = ro.overall_price_summary()
    average = summary.get("average_list_price")
    bucket = int(round(float(average) / 50000.0)) * 50000 if average else None
    top_hood = hoods[0][0] if hoods else None
    examples = []
    if top_hood and bucket:
        examples.append(f"'{top_hood} homes under ${bucket:,}'")
    if bucket:
        examples.append(f"'cheapest homes under ${bucket:,}'")
    if safe_zones:
        examples.append(f"'homes in a {safe_zones[0]} flood zone'")

    return {"prompt_block": prompt_block, "examples": tuple(examples[:3])}


def maybe_refresh(ro, prefs: dict, *, now: Optional[float] = None) -> None:
    """Refresh the cache if the TTL has lapsed. Never raises — a stale or empty cache is
    always safer than letting a background convenience read break a real answer."""
    now = time.monotonic() if now is None else now
    # `at=None` unambiguously means "never refreshed" — using 0.0 as that sentinel would
    # make any small `now` (a test double, or a real clock that happens to start near
    # zero) look "still fresh" and skip the very first refresh.
    if _cache["at"] is not None and now - _cache["at"] <= VOCAB_TTL_SECONDS:
        return
    try:
        fresh = build(ro, prefs or {})
        _cache["prompt_block"], _cache["examples"], _cache["at"] = (
            fresh["prompt_block"], fresh["examples"], now)
    except Exception:  # noqa: BLE001 - diagnosis/vocabulary is best-effort, never fatal
        pass


def peek_prompt_block() -> str:
    """Whatever is currently cached, without ever touching a store. Empty before the first
    successful refresh."""
    return _cache["prompt_block"]


def peek_examples() -> tuple[str, ...]:
    return _cache["examples"]


def safe_clarify_with_examples() -> str:
    """reasoning_contract.SAFE_CLARIFY, plus live example queries when the cache is warm.
    Falls back to the bare string when it is not — byte-identical to today's behavior."""
    examples = peek_examples()
    base = reasoning_contract.SAFE_CLARIFY
    if not examples:
        return base
    return chat_policy.clamp_reply(base + " For example: " + "; ".join(examples) + ".")


def reset_cache() -> None:
    """Test hook / config-reload hook, mirrors chat.reset_throttle() and llm.reset_cache()."""
    _cache["prompt_block"], _cache["examples"], _cache["at"] = "", (), None
