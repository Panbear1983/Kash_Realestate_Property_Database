"""Natural-language query mode — 'talk to your data'.

The LLM (Codex/ChatGPT via OAuth by default — see kash.llm) only translates the question
into a structured query spec against a fixed schema. kash.query runs that spec locally
and we format the rows deterministically. The model never writes SQL or sees the DB.
"""
from __future__ import annotations

from .schema import FIELD_ORDER

# JSON Schema handed to the backend via codex --output-schema (forces structured output).
SPEC_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "filters": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"field": {"type": "string"},
                           "op": {"type": "string"},
                           "value": {"type": "string"}},
            "required": ["field", "op", "value"]}},
        "sort": {"type": "string"},
        "order": {"type": "string"},
        "limit": {"type": "integer"},
    },
    "required": ["filters", "sort", "order", "limit"],
}

_OP_ALIAS = {"eq": "=", "equals": "=", "=": "=", "ne": "!=", "neq": "!=", "!=": "!=",
             "lt": "<", "<": "<", "lte": "<=", "le": "<=", "<=": "<=",
             "gt": ">", ">": ">", "gte": ">=", "ge": ">=", ">=": ">=",
             "contains": "contains", "like": "contains", "~": "contains"}
_FIELD_ALIAS = {"price": "list_price", "bedrooms": "beds", "bathrooms": "baths",
                "zip_code": "zip", "zipcode": "zip", "square_feet": "sqft",
                "sqfootage": "sqft", "school_rating": "school_gs_rating"}


def _prompt(question: str) -> str:
    return (
        "You translate a Staten Island home-buyer's question into a query spec for a "
        "property database. Output ONLY JSON matching the schema.\n"
        f"Filter/sort columns: {', '.join(FIELD_ORDER)}.\n"
        "Operators — use these exact tokens: =, !=, <, <=, >, >=, contains.\n"
        "Enums: status[active,pending,attorney_review,off_market,sold]; "
        "view_priority[now,soon,worth,call,watch,skip]; tier[S,A,B,C,W,X]; "
        "property_type[sf_attached,sf_semi,sf_detached,2fam_detached,2fam_semi,"
        "2fam_colonial,condo].\n"
        "Use column 'list_price' for price (integer, no $ or commas). Values as strings. "
        "Default sort 'rank' ascending (1=best); for 'cheapest' sort list_price asc; for "
        "'flood risk' filter flood_zone!=X.\n"
        f"Question: {question}"
    )


def route(config=None, override=None, job="chat"):
    """Build the backend ladder for one request.

    `config` is the whole preferences dict (we pull the `llm:` block out of it); `override` pins
    a single backend by name, e.g. from a per-user `model` command. Defaults to the `chat` job,
    whose ladder is ordered for latency because a user is waiting on the reply. Read `.chosen`
    off the returned object after a call to find out which rung actually answered.
    """
    from . import llm
    return llm.route((config or {}).get("llm"), override, job)


def get_backend(config=None):
    """Backward-compatible alias — returns the same ladder `route()` builds."""
    return route(config)


def available(config=None, override=None) -> tuple[bool, str]:
    try:
        return route(config, override).available()
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def answer(question: str, store, config=None, backend=None, override=None):
    """Return (message, rows). Raises RuntimeError if no backend is available."""
    from . import query
    backend = backend or route(config, override)
    ok, why = backend.available()
    if not ok:
        raise RuntimeError(f"NL backend ({backend.name}) unavailable: {why}")

    spec = backend.query_spec(_prompt(question), SPEC_SCHEMA)
    filters = []
    for f in spec.get("filters", []):
        field = _FIELD_ALIAS.get(f.get("field"), f.get("field"))
        op = _OP_ALIAS.get(str(f.get("op")).lower(), f.get("op"))
        filters.append({"field": field, "op": op, "value": f.get("value")})
    sort = _FIELD_ALIAS.get(spec.get("sort"), spec.get("sort") or "rank")
    order = spec.get("order") or "asc"
    limit = int(spec.get("limit") or 20)

    try:
        rows = query.run(store, filters=filters, sort=sort, order=order, limit=min(limit, 50))
    except ValueError as e:
        return (f"I built an invalid query ({e}). Try rephrasing.", [])

    crit = ", ".join(f"{f['field']}{f['op']}{f['value']}" for f in filters) or "all listings"
    return (f"{len(rows)} match — {crit} (sort {sort} {order})", rows)


# --- conversational mode (for the Telegram bot): chat OR query, model decides ---

CHAT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "mode": {"type": "string", "enum": ["chat", "query"]},
        "reply": {"type": "string"},
        "filters": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"field": {"type": "string"}, "op": {"type": "string"},
                           "value": {"type": "string"}},
            "required": ["field", "op", "value"]}},
        "sort": {"type": "string"}, "order": {"type": "string"}, "limit": {"type": "integer"},
    },
    "required": ["mode", "reply", "filters", "sort", "order", "limit"],
}


def _chat_prompt(message: str) -> str:
    return (
        "You are Kash, a warm, concise Staten Island real-estate assistant chatting over "
        "Telegram. You can look up a local property database. Choose a mode:\n"
        "- mode='chat' for greetings, small talk, thanks, or 'what can you do': write a "
        "short friendly 'reply' (1-2 sentences) and suggest one example question; set "
        "filters=[], sort='rank', order='asc', limit=20.\n"
        "- mode='query' when they ask about homes/listings/prices/areas/schools/flood: fill "
        "filters, sort, order, limit, and put a short natural lead-in in 'reply' "
        "(e.g. 'Here are the cheapest homes under $700k:').\n"
        "For 'show all', 'everything', or 'list all', use filters=[] and limit=100.\n"
        f"Columns: {', '.join(FIELD_ORDER)}.\n"
        "Operators (exact tokens): =, !=, <, <=, >, >=, contains. Enums: tier[S,A,B,C,W,X]; "
        "status[active,pending,attorney_review,off_market,sold]; property_type[sf_attached,"
        "sf_semi,sf_detached,2fam_detached,2fam_semi,2fam_colonial,condo]. Use 'list_price' "
        "for price (integer). 'flood risk' means flood_zone!=X. Values as strings.\n\n"
        f"User: {message}"
    )


def converse(message: str, store, config=None, backend=None, override=None):
    """Return (reply_text, rows). For chat, rows is empty. Raises if no backend is available."""
    from . import query
    backend = backend or route(config, override)
    ok, why = backend.available()
    if not ok:
        raise RuntimeError(f"NL backend ({backend.name}) unavailable: {why}")

    spec = backend.query_spec(_chat_prompt(message), CHAT_SCHEMA)
    reply = (spec.get("reply") or "").strip()
    if spec.get("mode") != "query":
        return (reply or "Hi! Ask me about Staten Island listings.", [])

    filters = []
    for f in spec.get("filters", []):
        field = _FIELD_ALIAS.get(f.get("field"), f.get("field"))
        op = _OP_ALIAS.get(str(f.get("op")).lower(), f.get("op"))
        filters.append({"field": field, "op": op, "value": f.get("value")})
    sort = _FIELD_ALIAS.get(spec.get("sort"), spec.get("sort") or "rank")
    order = spec.get("order") or "asc"
    limit = int(spec.get("limit") or 20)
    try:
        rows = query.run(store, filters=filters, sort=sort, order=order, limit=min(limit, 100))
    except ValueError as e:
        return (f"I couldn't build that query ({e}). Try rephrasing?", [])
    if not rows:
        return ("Nothing matches that one — want to loosen it a bit?", [])
    return (reply or f"{len(rows)} matches:", rows)
