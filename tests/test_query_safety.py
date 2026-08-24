#!/usr/bin/env python3
"""Characterization tests for the safe query layer — the thing the Telegram chat path will
sit on top of. No network, no LLM, temp/in-memory DB only.

These pin the guarantees kash/query.py already makes (whitelisted columns and operators,
every value bound as a parameter) so that opening the layer to untrusted Telegram input
cannot quietly erode them. They also pin three *gaps* that kash/chat_policy.py exists to
close, so that if query.py is ever hardened directly, the duplicate guard is noticed
rather than left in place forever.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import query                                  # noqa: E402
from kash.schema import FIELD_ORDER                     # noqa: E402
from kash.store import Store                            # noqa: E402


def _pool(n=5):
    store = Store(":memory:")
    for i in range(n):
        store.upsert({"street_address": f"{i} Test Street", "zip": "10308",
                      "list_price": 700000 + i * 1000, "status": "active",
                      "my_notes": "seller is motivated" if i == 0 else None}, "fixture")
    return store


# --- the guarantees that must hold ----------------------------------------------------------

def test_unknown_column_is_rejected():
    for field in ("sqlite_master", "password", "listings; DROP TABLE listings"):
        try:
            query.build([{"field": field, "op": "=", "value": "x"}], None, "asc", 10)
            raise AssertionError(f"accepted unknown field {field!r}")
        except ValueError as e:
            assert "unknown field" in str(e)


def test_unknown_operator_is_rejected():
    for op in ("OR 1=1", "IS", "GLOB", ""):
        try:
            query.build([{"field": "zip", "op": op, "value": "x"}], None, "asc", 10)
            raise AssertionError(f"accepted unknown op {op!r}")
        except ValueError as e:
            assert "unknown operator" in str(e)


def test_unknown_sort_field_is_rejected():
    try:
        query.build([], "rank; DROP TABLE listings", "asc", 10)
        raise AssertionError("accepted unknown sort field")
    except ValueError as e:
        assert "unknown sort field" in str(e)


def test_values_are_bound_never_interpolated():
    """The injection guarantee: a hostile value reaches SQL only as a parameter."""
    hostile = "'; DROP TABLE listings; --"
    sql, params = query.build(
        [{"field": "neighborhood", "op": "=", "value": hostile}], None, "asc", 10)
    assert hostile not in sql, sql
    assert sql.count("?") == len(params) == 1
    assert params == [hostile]


def test_contains_binds_the_wildcards_too():
    sql, params = query.build(
        [{"field": "neighborhood", "op": "contains", "value": "Kills"}], None, "asc", 10)
    assert "LIKE ?" in sql
    assert params == ["%Kills%"]


def test_a_hostile_value_actually_runs_and_leaves_the_table_intact():
    store = _pool()
    rows = query.run(store, filters=[{"field": "neighborhood", "op": "=",
                                      "value": "'; DROP TABLE listings; --"}])
    assert rows == []
    assert store.count() == 5, "table was damaged by a bound value"
    store.close()


def test_order_direction_cannot_be_injected():
    sql, _ = query.build([], "rank", "asc; DROP TABLE listings", 10)
    # The ORDER BY also sorts NULLs last now (see kash/query.py); the point of this test is
    # that nothing from `order` reaches the SQL but a bare ASC/DESC.
    assert sql.endswith('ORDER BY "rank" IS NULL, "rank" ASC LIMIT 10'), sql
    assert "DROP TABLE" not in sql


# --- the gaps chat_policy.py closes ---------------------------------------------------------
# Each of these is current, verified behaviour of query.py. They are not bugs *in* query.py —
# its contract is that callers pass a validated spec. They are the reason an LLM-produced spec
# must be sanitized before it gets here.

def test_gap_negative_limit_means_unlimited():
    """SQLite reads LIMIT -1 as 'no limit'. An LLM returning -1 would dump the whole pool."""
    store = _pool()
    assert len(query.run(store, filters=[], limit=-1)) == 5
    sql, _ = query.build([], None, "asc", -1)
    assert sql.endswith("LIMIT -1")
    store.close()


def test_gap_non_numeric_value_raises_typeerror_not_valueerror():
    """kash.nl catches ValueError only, so a None value would escape as an uncaught TypeError."""
    try:
        query.build([{"field": "list_price", "op": "<", "value": None}], None, "asc", 10)
        raise AssertionError("expected a raise")
    except TypeError:
        pass
    except ValueError:
        raise AssertionError("now raises ValueError — chat_policy's coercion may be redundant")


def test_gap_private_columns_are_queryable():
    """query.py whitelists all 88 schema columns, including the buyer's private notes.

    Filtering on a column is an oracle even when the column is never rendered: 'my_notes
    contains divorce' returning one row versus none leaks the note's content one bit at a
    time. This is why chat_policy narrows the set rather than relying on the renderer.
    """
    assert "my_notes" in FIELD_ORDER
    store = _pool()
    hits = query.run(store, filters=[{"field": "my_notes", "op": "contains",
                                      "value": "motivated"}])
    assert len(hits) == 1, "private column is filterable through the query layer"
    store.close()


def test_or_group_values_are_bound_never_interpolated():
    hostile = "'; DROP TABLE listings; --"
    sql, params = query.build(
        [{"field": "neighborhood", "op": "=", "value": "",
          "values": ["Great Kills", hostile]}], None, "asc", 10)
    assert hostile not in sql, sql
    assert sql.count("?") == len(params) == 2
    assert " OR " in sql and "(" in sql
    assert params == ["Great Kills", hostile]


def test_or_group_contains_binds_wildcards_per_value():
    sql, params = query.build(
        [{"field": "neighborhood", "op": "contains", "value": "",
          "values": ["Kills", "dale"]}], None, "asc", 10)
    assert params == ["%Kills%", "%dale%"]
    assert sql.count("LIKE ?") == 2


def test_or_group_runs_and_leaves_the_table_intact():
    store = _pool()
    rows = query.run(store, filters=[{"field": "neighborhood", "op": "=", "value": "",
                                      "values": ["A", "'; DROP TABLE listings; --"]}])
    assert rows == []
    assert store.count() == 5, "table damaged"
    store.close()


def test_aggregate_builder_rejects_unknown_parts():
    for args in (("median", "list_price", ""), ("avg", "my_notes", ""),
                 ("count", "", "street_address")):
        try:
            query.build_aggregate([], *args)
            raise AssertionError(f"accepted {args}")
        except ValueError:
            pass


def test_aggregate_sql_shape_is_fixed_and_parameterized():
    sql, params = query.build_aggregate(
        [{"field": "status", "op": "=", "value": "active"}],
        "avg", "list_price", "neighborhood")
    assert sql.startswith('SELECT "neighborhood" AS grp, AVG("list_price") AS value')
    assert 'GROUP BY "neighborhood"' in sql
    assert f"LIMIT {query.MAX_AGGREGATE_GROUPS}" in sql
    assert '"list_price" IS NOT NULL' in sql
    assert params == ["active"]


def test_aggregate_runs_against_a_real_pool():
    store = _pool()
    rows = query.run_aggregate(store, [], "count")
    assert int(rows[0]["value"]) == 5
    rows = query.run_aggregate(store, [], "avg", "list_price")
    assert int(rows[0]["n"]) == 5
    assert float(rows[0]["value"]) == 702000.0
    store.close()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — query layer binds values and whitelists columns")
