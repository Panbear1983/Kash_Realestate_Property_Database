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
            },
            "required": ["field", "op", "value"],
        }},
        "sort": {"type": "string"},
        "order": {"type": "string"},
        "limit": {"type": "integer"},
        "web_queries": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_WEB_QUERIES,
        },
    },
    "required": [
        "route", "reply", "filters", "sort", "order", "limit", "web_queries",
    ],
}

WEB_ANSWER_SCHEMA = {
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
    valid: bool = False


def prompt(message: str, columns: list[str]) -> str:
    """Build the routing prompt.  It contains schema names, never listing rows or tools."""
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
        "Put a brief safe clarification in reply.\n"
        "Never select web_research merely because a question is difficult. Never invent "
        "database rows or web findings. There are no write/action tools.\n"
        f"Database columns (names only): {', '.join(columns)}.\n"
        "Database operators: =, !=, <, <=, >, >=, contains. Defaults: filters=[], sort=rank, "
        "order=asc, limit=20. Use empty/default values for fields irrelevant to the route.\n\n"
        f"User: {message}"
    )


def parse(spec) -> RouteSpec:
    """Validate the strict model shape. Anything malformed fails to ``clarify``."""
    if not isinstance(spec, dict) or set(spec) != set(ROUTE_SCHEMA["required"]):
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
    return RouteSpec(
        route=route,
        reply=spec["reply"],
        filters=spec["filters"],
        sort=spec["sort"],
        order=spec["order"],
        limit=spec["limit"],
        web_queries=tuple(queries),
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
