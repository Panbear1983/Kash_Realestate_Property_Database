#!/usr/bin/env python3
"""count()/diagnose_empty()/aggregate_select(): the empty-result diagnosis path. All
COUNT-only — never a second row-returning query. No network, in-memory DB only."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import query  # noqa: E402
from kash.store import Store  # noqa: E402


def _pool():
    store = Store(":memory:")
    rows = [
        # zip 10308, beds 3-5, price 600k-900k — deliberately overlapping so single-filter
        # drops have a real, checkable answer.
        {"beds": "3", "list_price": 600000, "zip": "10308"},
        {"beds": "3", "list_price": 650000, "zip": "10308"},
        {"beds": "4", "list_price": 700000, "zip": "10308"},
        {"beds": "4", "list_price": 750000, "zip": "10312"},
        {"beds": "5", "list_price": 900000, "zip": "10312"},
    ]
    for i, r in enumerate(rows):
        store.upsert({"street_address": f"{i} Test St", "status": "active", **r}, "fixture")
    return store


def test_count_matches_run_length_for_the_same_filters():
    store = _pool()
    filters = [{"field": "zip", "op": "=", "value": "10308"}]
    assert query.count(store, filters) == len(query.run(store, filters=filters, limit=100))


def test_count_with_no_filters_is_the_whole_table():
    store = _pool()
    assert query.count(store) == 5


def test_count_rejects_unknown_field_or_operator_like_build_does():
    store = _pool()
    for bad in ([{"field": "nope", "op": "=", "value": "x"}],
               [{"field": "zip", "op": "nope", "value": "x"}]):
        try:
            query.count(store, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad} must be rejected")


def test_diagnose_empty_finds_the_single_filter_to_drop():
    store = _pool()
    # zip=10308 (3 rows) AND beds>=5 (1 row, in 10312) -> 0 rows together.
    filters = [{"field": "zip", "op": "=", "value": "10308"},
              {"field": "beds", "op": ">=", "value": "5"}]
    assert query.count(store, filters) == 0
    diag = query.diagnose_empty(store, filters)
    assert diag, "at least one dropped-filter alternative should unlock rows"
    best_filter, n = diag[0]
    # Dropping beds>=5 (keeping zip=10308) leaves 3 rows; dropping zip=10308 (keeping
    # beds>=5) leaves 1. Best-first means the BIGGER unlock — beds — leads.
    assert best_filter["field"] == "beds" and n == 3
    assert diag[1][0]["field"] == "zip" and diag[1][1] == 1


def test_diagnose_empty_returns_up_to_two_best_suggestions_best_first():
    store = _pool()
    filters = [{"field": "zip", "op": "=", "value": "99999"},   # unlocks 0 alone
              {"field": "beds", "op": ">=", "value": "99"}]     # unlocks 0 alone
    assert query.diagnose_empty(store, filters) == [], \
        "neither filter alone yields any rows, so nothing to suggest"

    # zip=10308 dropped alone leaves beds>=99 -> 0 rows (not suggested); beds>=99 dropped
    # alone leaves zip=10308 -> 3 rows (the only real suggestion here).
    filters2 = [{"field": "zip", "op": "=", "value": "10308"},
               {"field": "beds", "op": ">=", "value": "99"}]
    diag = query.diagnose_empty(store, filters2)
    assert len(diag) == 1 and diag[0][0]["field"] == "beds" and diag[0][1] == 3


def test_diagnose_empty_is_bounded_by_max_filters():
    store = _pool()
    filters = [{"field": "zip", "op": "=", "value": "10308"}] * 7   # over the 6-filter cap
    assert query.diagnose_empty(store, filters) == []


def test_diagnose_empty_returns_nothing_for_an_empty_or_impossible_filter_list():
    store = _pool()
    assert query.diagnose_empty(store, []) == []


def test_describe_filter_renders_readable_operators():
    assert query.describe_filter({"field": "beds", "op": ">=", "value": "4"}) == "beds ≥ 4"
    assert query.describe_filter({"field": "zip", "op": "=", "value": "10308"}) == "zip = 10308"
    assert query.describe_filter(
        {"field": "neighborhood", "op": "contains", "value": "Kills"}
    ) == "neighborhood contains Kills"


def test_aggregate_select_returns_query_shaped_rows_not_full_listing_rows():
    """The bug this closes: execute_select decodes every row against FIELD_ORDER and
    raises IndexError on a COUNT(*)-shaped result."""
    store = _pool()
    try:
        store.execute_select("SELECT COUNT(*) AS n FROM listings", ())
    except (IndexError, KeyError):
        pass
    else:
        raise AssertionError("execute_select was expected to choke on an aggregate query")

    rows = store.aggregate_select("SELECT COUNT(*) AS n FROM listings", ())
    assert rows == [{"n": 5}]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
