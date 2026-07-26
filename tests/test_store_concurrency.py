#!/usr/bin/env python3
"""Concurrent access to the pool.

Usage metering writes from the chat path — the Textual worker thread and the Telegram
bridge — against a database the 07:00 update job also writes. Before WAL and busy_timeout,
a collision raised `database is locked` immediately rather than waiting.
"""
import os
import sqlite3
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.store import Store  # noqa: E402


def fresh_path():
    return os.path.join(tempfile.mkdtemp(), "t.db")


def row(i):
    return {"street_address": f"{i} Test Ave", "zip": "10308", "list_price": 700000,
            "beds": "3", "baths": "2", "property_type": "sf_detached"}


# --- pragmas --------------------------------------------------------------------------------

def test_journal_mode_is_wal():
    s = Store(fresh_path())
    assert s.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_busy_timeout_is_set():
    s = Store(fresh_path())
    assert s.conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000


# --- concurrency ----------------------------------------------------------------------------

def test_two_connections_can_write_without_locking():
    """The failure this prevents: a chat message during the nightly run raising instead of
    waiting its turn."""
    path = fresh_path()
    a, b = Store(path), Store(path)
    a.upsert(row(1), source="test")
    b.upsert(row(2), source="test")
    a.upsert(row(3), source="test")
    assert a.count() == 3 and b.count() == 3


def test_a_reader_is_not_blocked_by_a_writer():
    path = fresh_path()
    writer, reader = Store(path), Store(path)
    writer.upsert(row(1), source="test")
    # Open a write transaction and hold it, then read from the other connection.
    writer.conn.execute("BEGIN IMMEDIATE")
    writer.conn.execute("UPDATE listings SET list_price=1 WHERE 1=1")
    assert reader.count() == 1, "WAL must let the reader proceed mid-write"
    writer.conn.execute("ROLLBACK")


def test_interleaved_writes_from_threads_do_not_raise():
    path = fresh_path()
    Store(path)                      # create the schema once up front
    errors = []

    def worker(start):
        try:
            s = Store(path)
            for i in range(start, start + 12):
                s.upsert(row(i), source="test")
        except sqlite3.OperationalError as e:   # the exact failure being guarded against
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n * 100,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"locked errors under concurrent writes: {errors}"
    assert Store(path).count() == 48


def test_ledger_and_listings_can_be_written_concurrently():
    """The real shape of the collision: metering writes while enrichment writes listings."""
    from kash.ledger import Ledger
    path = fresh_path()
    a = Store(path)
    a.upsert(row(1), source="test")
    b = Store(path)
    lg = Ledger(b)
    for i in range(10):
        lg.record_failure(f"key-{i}", "flood_zone", "boom")
        a.upsert(row(100 + i), source="test")
    assert a.count() == 11
    assert len(lg.unresolved("flood_zone")) == 10


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
