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


def build(filters, sort: Optional[str], order: str, limit: int):
    where, params = [], []
    for f in filters:
        field, op, val = f["field"], f.get("op", "="), f["value"]
        if field not in _FIELDS:
            raise ValueError(f"unknown field: {field}")
        sop = _OPS.get(op)
        if not sop:
            raise ValueError(f"unknown operator: {op}")
        if sop == "LIKE":
            where.append(f'"{field}" LIKE ?')
            params.append(f"%{val}%")
        else:
            where.append(f'"{field}" {sop} ?')
            params.append(_cast(field, val))
    sql = "SELECT * FROM listings"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if sort:
        if sort not in _FIELDS:
            raise ValueError(f"unknown sort field: {sort}")
        direction = "DESC" if str(order).lower().startswith("d") else "ASC"
        sql += f' ORDER BY "{sort}" {direction}'
    sql += f" LIMIT {int(limit)}"
    return sql, params


def run(store, filters=None, sort: str = "rank", order: str = "asc", limit: int = 20):
    sql, params = build(filters or [], sort, order, limit)
    return store.execute_select(sql, params)


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
    # default limit high so a filter shows ALL matches (the table scrolls); use limit:N to cap.
    sort, order, limit, filt = "rank", "asc", 200, []
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
