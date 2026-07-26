#!/usr/bin/env python3
"""Snapshots must contain the data. Regression test for the WAL backup bug.

Enabling WAL made `shutil.copy2` unsafe: committed rows live in pool.db-wal until a
checkpoint, and the checkpoint-on-close never fires because the Telegram bridge holds a
connection open permanently. The resulting snapshot passed PRAGMA integrity_check while
containing no listings at all.
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import backup      # noqa: E402
from kash.store import Store  # noqa: E402


def row(i):
    return {"street_address": f"{i} Test Ave", "zip": "10308", "list_price": 700000,
            "beds": "3", "baths": "2", "property_type": "sf_detached"}


def make(n=11, hold_second_connection=True):
    """A store with n rows and, by default, a second open connection — i.e. the live setup."""
    path = os.path.join(tempfile.mkdtemp(), "pool.db")
    s = Store(path)
    holder = Store(path) if hold_second_connection else None   # noqa: F841 — must stay open
    for i in range(n):
        s.upsert(row(i), source="test")
    return s, path


# --- the regression -------------------------------------------------------------------------

def test_snapshot_captures_rows_still_in_the_wal():
    """The exact failure: a second connection prevents checkpointing, so a file copy loses
    everything. This asserted 11 == 0 before the fix."""
    s, path = make()
    dest = backup.snapshot(path)
    assert dest and not str(dest).startswith("backup failed"), dest
    got = sqlite3.connect(dest).execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    assert got == s.count() == 11, f"backup has {got} rows, live has {s.count()}"


def test_an_empty_snapshot_is_reported_as_failure_not_success():
    """integrity_check says 'ok' for an empty database, so it cannot be the only check."""
    ok, why = backup.verify(os.path.join(tempfile.mkdtemp(), "empty.db"))
    assert ok is False and "does not exist" in why
    blank = os.path.join(tempfile.mkdtemp(), "blank.db")
    sqlite3.connect(blank).execute("CREATE TABLE listings (match_key TEXT)")
    ok, why = backup.verify(blank)
    assert ok is False and "empty" in why


def test_snapshot_survives_writes_from_another_connection():
    s, path = make(n=5)
    other = Store(path)
    for i in range(100, 108):
        other.upsert(row(i), source="test")
    dest = backup.snapshot(path)
    got = sqlite3.connect(dest).execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    assert got == s.count() == 13


# --- verification behaviour -----------------------------------------------------------------

def test_verify_accepts_a_good_snapshot():
    _, path = make()
    ok, detail = backup.verify(backup.snapshot(path))
    assert ok is True and "listings=11" in detail


def test_snapshot_preserves_user_protected_values():
    """The 35 curated rows are the only irreplaceable data; this is what backups exist for."""
    from kash.dedup import match_key
    s, path = make(n=1)
    s.update_fields(match_key(s.all()[0]),
                    {"my_notes": "private note", "analysis": "curated", "tier": "S"})
    dest = backup.snapshot(path)
    c = sqlite3.connect(dest)
    c.row_factory = sqlite3.Row
    r = dict(c.execute("SELECT my_notes, analysis, tier FROM listings").fetchone())
    assert r == {"my_notes": "private note", "analysis": "curated", "tier": "S"}


def test_snapshot_is_not_world_readable():
    _, path = make(n=1)
    dest = backup.snapshot(path)
    assert oct(os.stat(dest).st_mode)[-3:] == "600"


def test_missing_source_returns_none():
    assert backup.snapshot(os.path.join(tempfile.mkdtemp(), "nope.db")) is None
    assert backup.snapshot("") is None


def test_snapshot_does_not_modify_the_source():
    s, path = make(n=3)
    before = s.count()
    mtime = os.path.getmtime(path)
    backup.snapshot(path)
    assert s.count() == before
    assert os.path.getmtime(path) == mtime, "the backup must not write to the source"


def test_pruning_keeps_only_the_requested_number():
    _, path = make(n=1)
    for _ in range(4):
        backup.snapshot(path, keep=2)
    import glob
    kept = glob.glob(os.path.join(os.path.dirname(path), "backups", "pool-*.db"))
    assert len(kept) <= 2, f"expected pruning to 2, found {len(kept)}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
