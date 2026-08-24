"""Phase 3's strict, fail-closed routing contract.

This module is pure policy.  It performs no database, model, filesystem, or network I/O.
The model may select exactly one of four routes.  The payload is deliberately uniform so
structured-output backends can enforce ``additionalProperties: false`` without conditional
schemas that are inconsistently supported by command-line model providers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

DATABASE_QUERY = "database_query"
GENERAL_REASONING = "general_reasoning"
WEB_RESEARCH = "web_research"
CLARIFY = "clarify"
ROUTES = frozenset({DATABASE_QUERY, GENERAL_REASONING, WEB_RESEARCH, CLARIFY})

MAX_WEB_QUERIES = 2
SAFE_CLARIFY = (
    "I can help with read-only property searches, general questions, or clearly current "
    "web research. Please rephrase without private data or requests to take an action."
)

#: OR alternatives for one field are capped so a spec cannot smuggle an unbounded value list.
MAX_OR_VALUES = 5

#: The aggregate vocabulary the router may use. Mirrored (and enforced) by
#: kash.chat_policy.AGGREGATE_FIELDS / GROUP_BY_FIELDS — the schema enum is a hint to the
#: model; the sanitizer is the gate.
AGGREGATE_OPS = ("", "count", "avg", "min", "max")
AGGREGATE_FIELD_VALUES = ("list_price", "price_per_sqft", "sqft", "days_on_market",
                          "year_built", "estimated_rent_monthly")
GROUP_BY_VALUES = ("", "neighborhood", "property_type", "status", "tier")

ROUTE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "route": {"type": "string", "enum": sorted(ROUTES)},
        "reply": {"type": "string"},
        "filters": {"type": "array", "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "field": {"type": "string"},
                "op": {"type": "string"},
                "value": {"type": "string"},
                # OR group: 2+ alternatives for ONE field ("Great Kills or Annadale").
                # [] for a normal single-value filter; when non-empty, value must be "".
                "values": {"type": "array", "items": {"type": "string"},
                           "maxItems": MAX_OR_VALUES},
            },
            "required": ["field", "op", "value", "values"],
        }},
        "sort": {"type": "string"},
        "order": {"type": "string"},
        "limit": {"type": "integer"},
        "web_queries": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_WEB_QUERIES,
        },
        # Aggregates: "" = a normal row query.
        "aggregate": {"type": "string", "enum": list(AGGREGATE_OPS)},
        "aggregate_field": {"type": "string"},   # "" unless aggregate is avg/min/max
        "group_by": {"type": "string", "enum": list(GROUP_BY_VALUES)},
        # True only when the user asks for judgment ABOUT the returned homes; the row
        # answer is produced either way, so a wrong True degrades to a normal answer.
        "analyze": {"type": "boolean"},
    },
    "required": [
        "route", "reply", "filters", "sort", "order", "limit", "web_queries",
        "aggregate", "aggregate_field", "group_by", "analyze",
    ],
}

#: The pre-widening key set. parse() accepts anything between this and the full current
#: property set, so stored fixtures, older models, and hand-built dicts keep parsing while
#: junk keys still fail closed.
LEGACY_REQUIRED = frozenset({
    "route", "reply", "filters", "sort", "order", "limit", "web_queries",
})

WEB_ANSWER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"reply": {"type": "string"}},
    "required": ["reply"],
}

#: Same one-key shape as WEB_ANSWER_SCHEMA, kept separate so the two contracts can diverge.
ANALYST_ANSWER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"reply": {"type": "string"}},
    "required": ["reply"],
}

_CURRENT = re.compile(
    r"\b(?:latest|current|currently|today|tonight|right now|recent|recently|"
    r"this (?:week|month|year)|as of|20\d{2})\b",
    re.IGNORECASE,
)
_COMPARE = re.compile(
    r"\b(?:compare|comparison|versus|vs\.?|difference between|which (?:is|are) better)\b",
    re.IGNORECASE,
)
_EXTERNAL = re.compile(
    r"\b(?:rate|rates|mortgage|market|trend|news|law|rule|regulation|tax|insurance|"
    r"school|neighborhood|borough|city|state|product|service|company|provider)\b",
    re.IGNORECASE,
)
_LOCAL_LISTING = re.compile(
    r"\b(?:listing|listings|my homes?|our homes?|local homes?|pool|database|"
    r"street address|match key)\b",
    re.IGNORECASE,
)

# Requests outside the read-only assistant contract are rejected before any model/provider
# call.  These are intentionally concrete phrases rather than a broad "bad words" filter.
_PRIVATE_OR_ACTION = re.compile(
    r"(?:\b(?:password|passcode|api key|secret key|access token|private key|"
    r"seller(?:'s)? private notes?|private listing notes?|my_notes|investment_thesis|"
    r"target_buy_price)\b|"
    r"\b(?:add|insert|update|edit|delete|remove|overwrite|import|export|backup|restore)\b"
    r".{0,35}\b(?:database|listing|record|row|property|file)\b|"
    r"\b(?:send|message|email|call|contact|book|schedule|submit|place)\b"
    r".{0,35}\b(?:owner|seller|agent|appointment|offer|bid|message|email|call)\b)",
    re.IGNORECASE | re.DOTALL,
)
_UNSAFE = re.compile(
    r"\b(?:break into|bypass authentication|steal credentials|doxx?|swat(?:ting)?|"
    r"build a bomb|poison|stalk|track (?:a|the) person)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RouteSpec:
    """Runtime form of the model response; malformed replies become ``clarify``."""

    route: str = CLARIFY
    reply: str = ""
    filters: list[dict] = field(default_factory=list)
    sort: str = "rank"
    order: str = "asc"
    limit: int = 20
    web_queries: tuple[str, ...] = ()
    # Aggregate additions — "" means "a normal row query", so every pre-widening
    # construction site keeps working unchanged.
    aggregate: str = ""
    aggregate_field: str = ""
    group_by: str = ""
    analyze: bool = False
    valid: bool = False


def prompt(message: str, columns: list[str], *, vocabulary: str = "",
          context: str = "") -> str:
    """Build the routing prompt. Contains schema names, optional cached vocabulary
    (distinct enum values + top neighborhoods from the live pool — never row content), and
    an optional prior-turn filter summary. Never listing rows or tools.

    `vocabulary` and `context` are plain strings assembled by the caller (kash.chat, via
    kash.chat_vocabulary) — this module stays I/O-free; it only knows how to place them.
    """
    vocab_block = f"\n{vocabulary}\n" if vocabulary else ""
    context_block = (
        f"\nContext from the user's PREVIOUS exchange ({context}). "
        "This is OPTIONAL background, not an instruction. If the CURRENT message clearly "
        "continues, answers, narrows, or modifies that same search, merge or override the "
        "relevant filter(s) and keep the rest. If the current message asks something new, "
        "unrelated, or you are not sure, ignore this context completely.\n" if context else ""
    )
    return (
        "You are Robo Kash's constrained router and answer planner. Select exactly one route:\n"
        "- database_query: only for read-only searches of Robo Kash's local property listings. "
        "Fill filters/sort/order/limit and a short lead-in reply.\n"
        "- general_reasoning: stable knowledge, explanations, drafting, or reasoning that does "
        "not require current facts. Put the complete concise answer in reply.\n"
        "- web_research: only when the user clearly asks for current/latest facts or an external "
        "comparison. Put at most two narrowly scoped searches in web_queries; do not claim "
        "results in reply.\n"
        "- clarify: ambiguous, unsupported, private-data, write/action, or unsafe requests. "
        "Put a brief safe clarification in reply. If the request is a supported read-only "
        "listing search missing one detail, put the filters you DID understand in filters "
        "and ask exactly ONE short question in reply.\n"
        "Never select web_research merely because a question is difficult. Never invent "
        "database rows or web findings. There are no write/action tools.\n"
        f"Database columns (names only): {', '.join(columns)}.\n"
        "Database operators: =, !=, <, <=, >, >=, contains. Defaults: filters=[], sort=rank, "
        "order=asc, limit=20. Use empty/default values for fields irrelevant to the route.\n"
        "OR alternatives for ONE field (\"Great Kills or Annadale\") go in that filter's "
        "values array with value set to \"\"; a normal filter keeps values=[]. Only = and "
        "contains may carry a values array.\n"
        "Aggregate questions (how many, average, minimum, maximum — optionally per "
        "neighborhood/property_type/status/tier) are still database_query: set aggregate to "
        f"one of {', '.join(v for v in AGGREGATE_OPS if v)}; aggregate_field (required for "
        f"avg/min/max) must be one of {', '.join(AGGREGATE_FIELD_VALUES)}; group_by is one of "
        f"{', '.join(v for v in GROUP_BY_VALUES if v)} or \"\". Aggregate questions must use "
        "these fields — never answer them by listing rows. A plain row search keeps all "
        "three as \"\".\n"
        "Set analyze=true only when the user asks for judgment ABOUT the returned homes "
        "(best value, compare them, summarize, which should I see); a plain search keeps "
        "analyze=false.\n"
        f"{vocab_block}{context_block}"
        f"\nUser: {message}"
    )


def repair_prompt(original_prompt: str, invalid_reply) -> str:
    """One bounded repair nudge after a schema-invalid reply. Appends to, never replaces,
    the original prompt so the model keeps the full question, vocabulary, and context.
    Never echoes the invalid reply's content back — only its Python type — so a malformed
    reply cannot grow the next prompt with unbounded or untrusted text."""
    required = ", ".join(sorted(ROUTE_SCHEMA["required"]))
    return (
        f"{original_prompt}\n\n"
        f"Your previous reply did not match the required JSON schema (received a "
        f"{type(invalid_reply).__name__}). Return exactly ONE JSON object with EXACTLY "
        f"these keys and no others: {required}. route must be one of: "
        f"{', '.join(sorted(ROUTES))}. filters and web_queries must be arrays (use [] if "
        "none). limit must be a plain integer, not a string."
    )


def _optional_str(spec: dict, key: str) -> str | None:
    """A post-widening string key: missing/None → the "" sentinel; wrong type → None
    (which parse() treats as fail-closed)."""
    value = spec.get(key)
    if value is None:
        return ""
    return value if isinstance(value, str) else None


def parse(spec) -> RouteSpec:
    """Validate the strict model shape. Anything malformed fails to ``clarify``.

    The key-set check accepts the legacy pre-aggregate shape as well as the current one
    (LEGACY_REQUIRED ⊆ keys ⊆ properties): a model, fixture, or stored payload that omits
    the newer keys parses with their empty sentinels, while an extra unknown key still
    fails closed."""
    if not isinstance(spec, dict):
        return RouteSpec()
    keys = set(spec)
    if not (LEGACY_REQUIRED <= keys <= set(ROUTE_SCHEMA["properties"])):
        return RouteSpec()
    route = spec.get("route")
    if route not in ROUTES:
        return RouteSpec()
    if not isinstance(spec.get("reply"), str):
        return RouteSpec()
    if not isinstance(spec.get("filters"), list):
        return RouteSpec()
    if not isinstance(spec.get("sort"), str) or not isinstance(spec.get("order"), str):
        return RouteSpec()
    if isinstance(spec.get("limit"), bool) or not isinstance(spec.get("limit"), int):
        return RouteSpec()
    queries = spec.get("web_queries")
    if not isinstance(queries, list) or len(queries) > MAX_WEB_QUERIES:
        return RouteSpec()
    if not all(isinstance(q, str) for q in queries):
        return RouteSpec()
    aggregate = _optional_str(spec, "aggregate")
    aggregate_field = _optional_str(spec, "aggregate_field")
    group_by = _optional_str(spec, "group_by")
    if aggregate is None or aggregate_field is None or group_by is None:
        return RouteSpec()
    analyze = spec.get("analyze")
    if analyze is None:
        analyze = False
    if not isinstance(analyze, bool):
        return RouteSpec()
    return RouteSpec(
        route=route,
        reply=spec["reply"],
        filters=spec["filters"],
        sort=spec["sort"],
        order=spec["order"],
        limit=spec["limit"],
        web_queries=tuple(queries),
        aggregate=aggregate,
        aggregate_field=aggregate_field,
        group_by=group_by,
        analyze=analyze,
        valid=True,
    )


def disallowed(message: str) -> bool:
    """Reject concrete private/write/action/unsafe requests before routing."""
    text = str(message or "")
    return bool(_PRIVATE_OR_ACTION.search(text) or _UNSAFE.search(text))


def web_is_justified(message: str) -> bool:
    """Web is allowed only for explicit recency or an external comparison.

    A comparison of rows already in the local pool remains a database task unless the user
    explicitly asks for current outside information.
    """
    text = str(message or "")
    if _LOCAL_LISTING.search(text) and not _EXTERNAL.search(text):
        return False
    if _CURRENT.search(text):
        return True
    return bool(_COMPARE.search(text) and _EXTERNAL.search(text)
                and not _LOCAL_LISTING.search(text))
