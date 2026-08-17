#!/usr/bin/env python3
"""Sort order at the edges: empty values must never crowd out real ones.

The bug this pins was invisible and total. SQLite sorts NULLs first, so the dashboard's
default view — query.run(sort='rank', limit=100) against a pool of 35 ranked and 219
unranked rows — returned 100 unranked rows and not a single curated one. The buyer's own
top-35 list could not be reached from the table at all. No network.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import query  # noqa: E402
from kash.store import Store  # noqa: E402


def pool(ranked=3, unranked=20):
    """A pool shaped like the real one: a few ranked rows, many unranked."""
    s = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    for i in range(unranked):
        s.upsert({"street_address": f"{i} Unranked Ave", "zip": "10308",
                  "list_price": 600000 + i, "beds": "3", "baths": "2",
                  "property_type": "sf_detached"}, source="zillow")
    for i in range(ranked):
        r = {"street_address": f"{i} Curated St", "zip": "10308",
             "list_price": 700000 + i, "beds": "3", "baths": "2",
             "property_type": "sf_detached"}
        s.upsert(r, source="seed:docx")
        from kash.dedup import match_key
        s.update_fields(match_key(r), {"rank": i + 1}, allow_protected=True)
    return s


def test_curated_rows_survive_the_dashboard_limit():
    """The regression: with NULLs first, a LIMIT returned zero ranked rows."""
    s = pool(ranked=3, unranked=20)
    rows = query.run(s, sort="rank", limit=10)
    assert [r["rank"] for r in rows][:3] == [1, 2, 3]
    assert sum(r["rank"] is not None for r in rows) == 3


def test_empty_values_sort_last_ascending():
    s = pool(ranked=2, unranked=3)
    ranks = [r["rank"] for r in query.run(s, sort="rank", limit=100)]
    assert ranks == [1, 2, None, None, None]


def test_empty_values_sort_last_descending_too():
    """A row with no price must not top a 'most expensive first' list."""
    s = pool(ranked=2, unranked=1)
    s.upsert({"street_address": "9 Priceless Way", "zip": "10308", "beds": "3",
              "baths": "2", "property_type": "sf_detached"}, source="zillow")
    prices = [r["list_price"] for r in query.run(s, sort="list_price", order="desc",
                                                 limit=100)]
    assert prices[-1] is None, prices
    assert prices[0] == max(p for p in prices if p is not None)


def test_filters_still_apply_with_the_new_ordering():
    s = pool(ranked=2, unranked=5)
    rows = query.run(s, filters=[{"field": "list_price", "op": ">=", "value": 700000}],
                     sort="rank", limit=100)
    assert rows and all(r["list_price"] >= 700000 for r in rows)


def test_the_sort_column_is_still_whitelisted():
    for bad in ("rank; DROP TABLE listings", "not_a_column"):
        try:
            query.build([], bad, "asc", 10)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} must be rejected")


def test_no_sort_means_no_order_by():
    sql, _ = query.build([], None, "asc", 10)
    assert "ORDER BY" not in sql


def test_the_default_repl_limit_covers_the_whole_pool():
    """200 dated from an 88-row pool; the census-fed pool is several hundred rows, and a
    default that silently clips 'show me tier=A' misrepresents the database."""
    _, _, _, limit = query.parse_command("tier=A")
    assert limit >= 2000
    assert query.parse_command("tier=A limit:10")[3] == 10, "an explicit cap still wins"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
