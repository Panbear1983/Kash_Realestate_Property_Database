"""What a Telegram chat request is allowed to ask for, and how an LLM's answer is made safe.

Pure policy: no I/O, no database, no network, no model. Everything here is a function of its
arguments, which is what lets the chat path be tested with fakes.

`kash.query` already guarantees that columns and operators are whitelisted and values are
bound as parameters — SQL injection is not the exposure. Two other things are, once the spec
comes from a model driven by a stranger's message rather than from the owner at a dashboard:

1. **Crashes.** `kash.nl.converse` computes `int(spec["limit"])` outside its try block, and
   `query._cast` raises TypeError (not the ValueError nl catches) on a null value. Both reach
   the user as a traceback. `sanitize_spec` removes the whole class by validating types before
   the spec reaches query.py.

2. **Reading more than was intended.** `query.py` whitelists all 88 schema columns, including
   `my_notes`. A filter is an oracle even when the column is never rendered: asking for
   `my_notes contains divorce` and getting one row back versus none leaks the note a bit at a
   time. So the filterable set is narrowed here, at the spec, rather than left to the renderer.
   Separately, `LIMIT -1` is *unlimited* in SQLite, so an unclamped limit dumps the pool.

Note on scope: `analysis`, `tier` and `view_priority` are already sent to every approved
recipient by the outbound alert path (`notifications.format_listing_brief`). This module does
not change that and does not touch outbound. It is deliberately stricter about `analysis` for
*filtering*, for the oracle reason above; `tier`, `view_priority` and `rank` stay queryable
because they are already shared and because `rank` is the default sort.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as _field

from .query import _OPS
from .schema import FIELD_ORDER, INT_FIELDS, REAL_FIELDS

# --- limits ---------------------------------------------------------------------------------

MAX_MESSAGE_CHARS = 500     # inbound, before it is ever put in a prompt
MAX_REPLY_CHARS = 700       # the model's free-text 'reply', echoed to a human
MIN_LIMIT = 1
MAX_LIMIT = 100             # matches the ceiling kash.nl.converse already applies
DEFAULT_LIMIT = 20
DEFAULT_SORT = "rank"

#: How many listings one chat answer renders. The limit above bounds the *query*; this bounds
#: the *reply*. 100 briefs is roughly six Telegram messages, which reads as a flood rather
#: than an answer, so the reply says "showing 25 of 61" and lets the user narrow it.
MAX_ROWS_RENDERED = 25

# --- what is private ------------------------------------------------------------------------

#: Columns an approved-but-not-owner chat user may neither filter on nor sort by.
#: Three groups: free-text human judgment, the buyer's negotiating position (leaking a
#: target buy price to a counterparty is a concrete harm, not a theoretical one), and CRM
#: intent. Kept explicit rather than derived from schema.USER_PROTECTED, which answers a
#: different question — that set is about who may *write* a column during a provider merge.
PRIVATE_FIELDS = frozenset({
    # free-text judgment
    "my_notes", "analysis", "investment_thesis",
    "priority_note", "price_note", "bd_ba_note", "bid_note",
    # negotiating position and investment strategy
    "bid_estimate", "target_buy_price", "arv_estimate", "appreciation_pct", "brrrr_rating",
    # CRM / intent
    "favorite", "viewing_status", "viewing_date", "offer_status", "user_rating",
    "contacted_agent",
})

#: Everything a normal approved user may query.
CHAT_FIELDS = frozenset(FIELD_ORDER) - PRIVATE_FIELDS

OWNER = "owner"


def visible_fields(access_level: str | None = "read") -> frozenset[str]:
    """Columns this access level may filter and sort on."""
    return frozenset(FIELD_ORDER) if str(access_level or "").lower() == OWNER else CHAT_FIELDS


# --- alias maps -----------------------------------------------------------------------------
# Canonical copies of what kash.nl applies today. tests/test_chat_policy.py asserts these stay
# a superset of nl's private maps, so the two cannot drift apart unnoticed.

OP_ALIAS = {
    "eq": "=", "equals": "=", "=": "=", "==": "=",
    "ne": "!=", "neq": "!=", "!=": "!=", "<>": "!=",
    "lt": "<", "<": "<", "lte": "<=", "le": "<=", "<=": "<=",
    "gt": ">", ">": ">", "gte": ">=", "ge": ">=", ">=": ">=",
    "contains": "contains", "like": "contains", "~": "contains",
    "includes": "contains", "has": "contains",
}

FIELD_ALIAS = {
    "price": "list_price", "list price": "list_price",
    "bedrooms": "beds", "bedroom": "beds", "bed": "beds",
    "bathrooms": "baths", "bathroom": "baths", "bath": "baths",
    "zip_code": "zip", "zipcode": "zip", "postal_code": "zip",
    "square_feet": "sqft", "sqfootage": "sqft", "square_footage": "sqft",
    "school_rating": "school_gs_rating",
    "flood": "flood_zone",
}

_NUMERIC = frozenset(INT_FIELDS) | frozenset(REAL_FIELDS)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class SafeSpec:
    """A spec that kash.query can execute without raising."""
    filters: list[dict] = _field(default_factory=list)
    sort: str = DEFAULT_SORT
    order: str = "asc"
    limit: int = DEFAULT_LIMIT
    dropped: tuple[str, ...] = ()


# --- text ------------------------------------------------------------------------------------

def _clean(text, limit: int) -> str:
    """Strip control characters and truncate on a word boundary where one is close."""
    text = _CONTROL.sub("", str(text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.6 else cut).rstrip() + "…"


def clamp_message(text) -> str:
    """Normalize an inbound Telegram message before it is put into a prompt.

    Bounds prompt size (and so cost and latency) and removes control characters that would
    otherwise be echoed back into a terminal by the dashboard's access editor.
    """
    return _clean(text, MAX_MESSAGE_CHARS)


def clamp_reply(text) -> str:
    """Bound the model's free-text 'reply' before it is shown to a user."""
    return _clean(text, MAX_REPLY_CHARS)


# --- redaction ---------------------------------------------------------------------------------

def redact(row: dict, access_level: str | None = "read") -> dict:
    """Drop private columns from a row before anything renders it.

    Returns a plain dict with the private keys *absent* rather than blanked, because
    `notifications.format_listing_brief` is written against `row.get(...)` and omits a line
    whose value is missing. That lets the chat path reuse the existing formatter unchanged
    instead of growing a second copy of it — the outbound alert path keeps rendering exactly
    what it renders today.
    """
    if str(access_level or "").lower() == OWNER:
        return dict(row)
    return {k: v for k, v in row.items() if k not in PRIVATE_FIELDS}


def redact_rows(rows, access_level: str | None = "read") -> list[dict]:
    return [redact(row, access_level) for row in rows]


# --- spec -------------------------------------------------------------------------------------

def _canonical_field(raw) -> str:
    name = str(raw or "").strip().lower()
    return FIELD_ALIAS.get(name, name)


def _numeric_ok(value: str) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _clean_filter(raw, allowed: frozenset[str], dropped: list[str]) -> dict | None:
    if not isinstance(raw, dict):
        dropped.append("filter_not_an_object")
        return None

    field = _canonical_field(raw.get("field"))
    if not field:
        dropped.append("filter_without_field")
        return None
    if field not in allowed:
        # Same message whether the column is private or imaginary: confirming that `my_notes`
        # exists is itself a small leak.
        dropped.append(f"field_not_available:{field}")
        return None

    op = OP_ALIAS.get(str(raw.get("op", "=")).strip().lower())
    if op is None or op not in _OPS:
        dropped.append(f"unsupported_operator:{raw.get('op')}")
        return None

    value = raw.get("value")
    if value is None:
        dropped.append(f"null_value:{field}")     # would be TypeError inside query._cast
        return None
    if isinstance(value, (list, dict, tuple, set)):
        dropped.append(f"non_scalar_value:{field}")
        return None
    if isinstance(value, bool):
        value = "true" if value else "false"
    else:
        value = str(value).strip()
    if not value:
        dropped.append(f"empty_value:{field}")
        return None

    # A numeric column with a non-numeric value is the remaining crash path in query._cast.
    # 'contains' is exempt: it is a LIKE, so the value is never cast.
    if field in _NUMERIC and op != "contains" and not _numeric_ok(value):
        dropped.append(f"non_numeric_value:{field}={value}")
        return None

    return {"field": field, "op": op, "value": value}


def sanitize_spec(spec, access_level: str | None = "read") -> SafeSpec:
    """Turn a model-produced query spec into one that is safe to execute.

    Never raises and never returns a spec that makes `kash.query.build` raise: unusable parts
    are dropped, with a reason recorded in `.dropped` for logging, rather than failing the
    whole request. A spec that is entirely junk degrades to 'first 20 by rank', which is a
    reasonable answer to a question we could not parse.
    """
    dropped: list[str] = []
    if not isinstance(spec, dict):
        return SafeSpec(dropped=("spec_not_an_object",))

    allowed = visible_fields(access_level)

    raw_filters = spec.get("filters")
    filters: list[dict] = []
    if raw_filters is None:
        pass
    elif not isinstance(raw_filters, list):
        dropped.append("filters_not_a_list")
    else:
        for raw in raw_filters:
            cleaned = _clean_filter(raw, allowed, dropped)
            if cleaned is not None:
                filters.append(cleaned)

    sort = _canonical_field(spec.get("sort")) or DEFAULT_SORT
    if sort not in allowed:
        if sort != DEFAULT_SORT:
            dropped.append(f"sort_not_available:{sort}")
        sort = DEFAULT_SORT

    order = "desc" if str(spec.get("order") or "").strip().lower().startswith("d") else "asc"

    raw_limit = spec.get("limit")
    try:
        limit = int(float(raw_limit))
    except (TypeError, ValueError):
        if raw_limit is not None:
            dropped.append(f"bad_limit:{raw_limit}")
        limit = DEFAULT_LIMIT
    if limit < MIN_LIMIT:
        # LIMIT -1 is 'no limit' in SQLite — the whole pool, to a phone.
        dropped.append(f"limit_below_minimum:{limit}")
        limit = MIN_LIMIT
    elif limit > MAX_LIMIT:
        dropped.append(f"limit_above_maximum:{limit}")
        limit = MAX_LIMIT

    return SafeSpec(filters=filters, sort=sort, order=order, limit=limit,
                    dropped=tuple(dropped))
