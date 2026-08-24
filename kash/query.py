"""Safe, read-only query layer over the pool — shared by the command REPL and the
natural-language tool. Columns and operators are whitelisted; every value is bound as a
parameter, so no free-form SQL (or injection) ever reaches the database.
"""
from __future__ import annotations

import shlex
from typing import Optional

from .schema import BOOL_FIELDS, FIELD_ORDER, INT_FIELDS, REAL_FIELDS

_FIELDS = set(FIELD_ORDER)
_OPS = {"=": "=", "!=": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">=",
        "contains": "LIKE", "~": "LIKE"}
_TOKEN_OPS = ["<=", ">=", "!=", "<", ">", "=", "~"]  # order matters (longest first)


def _cast(field: str, value):
    if field in INT_FIELDS:
        return int(float(value))
    if field in REAL_FIELDS:
        return float(value)
    if field in BOOL_FIELDS:
        return 1 if str(value).lower() in ("true", "1", "yes", "y") else 0
    return value


def _where(filters) -> tuple[list[str], list]:
    where, params = [], []
    for f in filters:
        field, op = f["field"], f.get("op", "=")
        if field not in _FIELDS:
            raise ValueError(f"unknown field: {field}")
        sop = _OPS.get(op)
        if not sop:
            raise ValueError(f"unknown operator: {op}")
        values = f.get("values")
        if values:
            # An OR group is ONE filter entry: alternatives for a single field, still one
            # bound parameter per value. Parenthesized so it ANDs cleanly with the rest.
            if sop == "LIKE":
                where.append("(" + " OR ".join(f'"{field}" LIKE ?' for _ in values) + ")")
                params.extend(f"%{v}%" for v in values)
            else:
                where.append("(" + " OR ".join(f'"{field}" {sop} ?' for _ in values) + ")")
                params.extend(_cast(field, v) for v in values)
            continue
        val = f["value"]
        if sop == "LIKE":
            where.append(f'"{field}" LIKE ?')
            params.append(f"%{val}%")
        else:
            where.append(f'"{field}" {sop} ?')
            params.append(_cast(field, val))
    return where, params


def build(filters, sort: Optional[str], order: str, limit: int):
    where, params = _where(filters or [])
    sql = "SELECT * FROM listings"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if sort:
        if sort not in _FIELDS:
            raise ValueError(f"unknown sort field: {sort}")
        direction = "DESC" if str(order).lower().startswith("d") else "ASC"
        # Empty values sort LAST, in both directions. SQLite puts NULLs first by default,
        # which combined with LIMIT hid the pool's curated rows completely: sort='rank' with
        # 219 unranked rows and LIMIT 100 returned 100 NULL-rank rows and not one of the
        # 35 ranked ones — the dashboard's default view could not show them at all.
        sql += f' ORDER BY "{sort}" IS NULL, "{sort}" {direction}'
    sql += f" LIMIT {int(limit)}"
    return sql, params


def run(store, filters=None, sort: str = "rank", order: str = "asc", limit: int = 20):
    sql, params = build(filters or [], sort, order, limit)
    return store.execute_select(sql, params)


# --- allow-listed aggregates ------------------------------------------------------------------
# Shared with kash.chat_policy the same way _OPS is: chat_policy imports these and applies
# them at the spec, so a spec that reaches build_aggregate() is already clean — the raises
# below are belt-and-braces, exactly like _where()'s. tests assert these stay equal to
# reasoning_contract's model-facing copies.

_AGG_SQL = {"count": "COUNT(*)", "avg": 'AVG("{f}")', "min": 'MIN("{f}")', "max": 'MAX("{f}")'}
AGGREGATES = frozenset(_AGG_SQL)
AGGREGATE_FIELDS = frozenset({"list_price", "price_per_sqft", "sqft", "days_on_market",
                              "year_built", "estimated_rent_monthly"})
GROUP_BY_FIELDS = frozenset({"neighborhood", "property_type", "status", "tier"})
MAX_AGGREGATE_GROUPS = 20


def build_aggregate(filters, aggregate: str, aggregate_field: str,
                    group_by: str) -> tuple[str, list]:
    """One whitelisted aggregate over the same WHERE construction as build(). The SQL shape
    is fixed by code; the spec chooses only among the frozensets above."""
    if aggregate not in _AGG_SQL:
        raise ValueError(f"unknown aggregate: {aggregate}")
    if aggregate == "count":
        agg_sql = _AGG_SQL["count"]
    else:
        if aggregate_field not in AGGREGATE_FIELDS:
            raise ValueError(f"unknown aggregate field: {aggregate_field}")
        agg_sql = _AGG_SQL[aggregate].format(f=aggregate_field)
    if group_by and group_by not in GROUP_BY_FIELDS:
        raise ValueError(f"unknown group_by field: {group_by}")

    where, params = _where(filters or [])
    if aggregate != "count":
        # Mirror price_summary: NULL/empty values are excluded, not averaged as zero.
        where.append(f'"{aggregate_field}" IS NOT NULL')
    if group_by:
        sql = (f'SELECT "{group_by}" AS grp, {agg_sql} AS value, COUNT(*) AS n FROM listings')
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += (f' GROUP BY "{group_by}" ORDER BY value DESC'
                f" LIMIT {int(MAX_AGGREGATE_GROUPS)}")
    else:
        sql = f"SELECT {agg_sql} AS value, COUNT(*) AS n FROM listings"
        if where:
            sql += " WHERE " + " AND ".join(where)
    return sql, params


def run_aggregate(store, filters, aggregate: str, aggregate_field: str = "",
                  group_by: str = "") -> list[dict]:
    sql, params = build_aggregate(filters, aggregate, aggregate_field, group_by)
    return store.aggregate_select(sql, params)


def build_count(filters) -> tuple[str, list]:
    """Same WHERE-clause construction as build(); a count is invariant to sort/limit."""
    where, params = _where(filters or [])
    sql = "SELECT COUNT(*) AS n FROM listings"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return sql, params


def count(store, filters=None) -> int:
    sql, params = build_count(filters)
    rows = store.aggregate_select(sql, params)
    return int(rows[0]["n"]) if rows else 0


MAX_DIAGNOSIS_FILTERS = 6
MAX_DIAGNOSIS_SUGGESTIONS = 2
_OP_WORDS = {"=": "=", "!=": "≠", "<": "<", "<=": "≤", ">": ">", ">=": "≥",
            "contains": "contains"}


def describe_filter(f: dict) -> str:
    op = f.get("op")
    values = f.get("values")
    shown = " or ".join(str(v) for v in values) if values else f.get("value")
    return f"{f.get('field')} {_OP_WORDS.get(op, op)} {shown}"


def diagnose_empty(store, filters: list[dict], *, max_filters: int = MAX_DIAGNOSIS_FILTERS,
                   max_suggestions: int = MAX_DIAGNOSIS_SUGGESTIONS) -> list[tuple[dict, int]]:
    """For a filtered search that returned zero rows: which SINGLE filter, if dropped
    alone, would unlock the most matches. COUNT-only — never a second row-returning query,
    so this cannot show any row content beyond what the user already asked for and got
    zero of. Bounded to `max_filters` extra COUNT queries so a spec with many filters
    degrades to no diagnosis rather than a query-count explosion. Returns up to
    `max_suggestions` filters, best (highest unlocked count) first, among those that
    individually yield > 0 rows if dropped alone."""
    if not filters or len(filters) > max_filters:
        return []
    found = []
    for i in range(len(filters)):
        remaining = filters[:i] + filters[i + 1:]
        try:
            n = count(store, remaining)
        except Exception:  # noqa: BLE001 - diagnosis is best-effort, never fatal
            continue
        if n > 0:
            found.append((filters[i], n))
    found.sort(key=lambda pair: pair[1], reverse=True)
    return found[:max_suggestions]


def parse_filters(tokens) -> list[dict]:
    """Turn command-line tokens like ['tier=A', 'list_price<=750000', 'neighborhood~Kills']
    into filter dicts. `~` means contains."""
    out = []
    for t in tokens:
        for op in _TOKEN_OPS:
            if op in t:
                field, val = t.split(op, 1)
                out.append({
                    "field": field.strip(),
                    "op": "contains" if op == "~" else op,
                    "value": val.strip().strip("'\""),
                })
                break
    return out


def parse_command(line: str):
    """Parse a REPL query line into (filters, sort, order, limit).
    Recognizes bare `field OP value` tokens plus `sort:FIELD`, `order:desc`, `limit:N`."""
    toks = shlex.split(line)
    # default limit high so a filter shows ALL matches (the table scrolls); use limit:N to
    # cap. 200 dated from an 88-row pool — the census-fed pool is several hundred rows.
    sort, order, limit, filt = "rank", "asc", 2000, []
    for t in toks:
        if t.startswith("sort:"):
            sort = t.split(":", 1)[1]
        elif t.startswith("order:"):
            order = t.split(":", 1)[1]
        elif t.startswith("limit:"):
            limit = int(t.split(":", 1)[1])
        else:
            filt.append(t)
    return parse_filters(filt), sort, order, limit
