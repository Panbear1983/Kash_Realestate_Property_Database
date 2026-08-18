#!/usr/bin/env python3
"""The read-only boundary the Telegram chat path will hold. Temp DB, no network."""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import query                                      # noqa: E402
from kash.readonly import ReadOnlyStore, open_readonly       # noqa: E402
from kash.store import Store                                 # noqa: E402

WRITE_METHODS = ("upsert", "update_fields", "mark_notification_sent",
                 "seed_from_csv", "bootstrap", "_write", "_log", "close")


def _seeded(path=":memory:", n=3):
    store = Store(path)
    for i in range(n):
        store.upsert({"street_address": f"{i} Test Street", "zip": "10308",
                      "list_price": 700000 + i, "status": "active",
                      "price_history": [{"date": "2026-07-01", "price": 700000 + i}],
                      "favorite": True}, "fixture")
    return store


def _on_disk(n=3):
    path = os.path.join(tempfile.mkdtemp(), "pool.db")
    store = _seeded(path, n)
    store.close()
    return path


# --- API surface ----------------------------------------------------------------------------

def test_reads_pass_through():
    store = _seeded()
    ro = ReadOnlyStore(store)
    assert ro.count() == 3
    assert len(ro.all()) == 3
    assert len(ro.execute_select("SELECT * FROM listings")) == 3
    store.close()


def test_every_write_method_is_absent():
    ro = ReadOnlyStore(_seeded())
    for name in WRITE_METHODS:
        assert not hasattr(ro, name), f"ReadOnlyStore still exposes {name}()"


def test_overall_price_summary_includes_all_statuses_and_ignores_missing_prices():
    store = Store(":memory:")
    store.upsert({"street_address": "Active priced", "zip": "10308", "list_price": 700000,
                  "status": "active"}, "fixture")
    store.upsert({"street_address": "Inactive priced", "zip": "10308", "list_price": 900000,
                  "status": "inactive"}, "fixture")
    store.upsert({"street_address": "No price", "zip": "10308", "status": "active"}, "fixture")
    assert ReadOnlyStore(store).overall_price_summary() == {
        "listing_count": 2,
        "average_list_price": 800000.0,
    }


def test_the_connection_is_not_reachable():
    """The whole point: no .conn means no ad-hoc SQL past the query layer."""
    ro = ReadOnlyStore(_seeded())
    assert not hasattr(ro, "conn")
    try:
        ro.conn.execute("DELETE FROM listings")
        raise AssertionError("reached the connection through the wrapper")
    except AttributeError:
        pass


def test_slots_block_attaching_a_way_back_in():
    ro = ReadOnlyStore(_seeded())
    try:
        ro.conn = "smuggled"
        raise AssertionError("wrapper accepted a new attribute")
    except AttributeError:
        pass


def test_query_run_works_unchanged_through_the_wrapper():
    """kash.query only calls execute_select, so this must be a drop-in for Store."""
    store = _seeded()
    ro = ReadOnlyStore(store)
    direct = query.run(store, filters=[{"field": "status", "op": "=", "value": "active"}])
    wrapped = query.run(ro, filters=[{"field": "status", "op": "=", "value": "active"}])
    assert wrapped == direct
    assert len(wrapped) == 3
    store.close()


# --- connection-level enforcement -----------------------------------------------------------

def test_readonly_connection_refuses_writes():
    ro = open_readonly(_on_disk())
    assert ro.count() == 3
    try:
        ro._store.conn.execute("DELETE FROM listings")
        raise AssertionError("a mode=ro connection accepted a DELETE")
    except sqlite3.OperationalError as e:
        assert "readonly" in str(e).lower(), e


def test_readonly_connection_refuses_schema_changes():
    ro = open_readonly(_on_disk())
    for sql in ("DROP TABLE listings", "ALTER TABLE listings ADD COLUMN x TEXT",
                "UPDATE listings SET list_price=1"):
        try:
            ro._store.conn.execute(sql)
            raise AssertionError(f"a mode=ro connection accepted: {sql}")
        except sqlite3.OperationalError:
            pass


def test_readonly_open_will_not_create_a_pool():
    """mode=ro must not bring a database into being if the path is wrong."""
    missing = os.path.join(tempfile.mkdtemp(), "does_not_exist.db")
    try:
        open_readonly(missing)
        raise AssertionError("opened a database that does not exist")
    except sqlite3.OperationalError:
        pass
    assert not os.path.exists(missing), "opening created the file"


def test_readonly_decodes_rows_like_store_does():
    """JSON and bool columns must not diverge from what the dashboard sees."""
    path = _on_disk(1)
    writable = Store(path)
    expected = writable.all()[0]
    writable.close()

    row = open_readonly(path).all()[0]
    assert row["price_history"] == expected["price_history"]
    assert isinstance(row["price_history"], list), "JSON column not decoded"
    assert row["favorite"] is True and isinstance(row["favorite"], bool)
    assert set(row) == set(expected)


def test_readonly_get_matches_writable_get():
    path = _on_disk(2)
    writable = Store(path)
    key = writable.conn.execute("SELECT match_key FROM listings").fetchone()[0]
    expected = writable.get(key)
    writable.close()

    ro = open_readonly(path)
    assert ro.get(key) == expected
    assert ro.get("no-such-key") is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — chat path cannot write to the pool")
