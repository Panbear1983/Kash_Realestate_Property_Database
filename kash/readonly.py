"""Read-only view of the pool — the handle the Telegram chat path is allowed to hold.

`OPERATIONS.md` requires that Kash stay a read-only data core behind the shared bridge.
That is currently a convention: `Store` hands out `.conn`, so any code holding a store can
write to the pool, and the chat path will be driven by messages from people who are not the
owner. This module makes the restriction structural instead.

Two layers, deliberately:

* `ReadOnlyStore` narrows the *API surface* to the four methods a query needs. There is no
  `__getattr__` passthrough, so `.upsert(...)` or `.conn` raises AttributeError at the call
  site rather than succeeding. This is the layer tests assert against, and it works over an
  in-memory store.
* `open_readonly()` narrows the *connection*, opening SQLite with `mode=ro` so the database
  itself refuses a write. This is the layer that holds even if someone reaches past the
  wrapper, and it is what production should use.

`kash.query.run()` only ever calls `store.execute_select`, so a `ReadOnlyStore` is a drop-in
wherever a `Store` is passed today — no change to query.py is needed.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

from .schema import FIELD_ORDER
from .store import _from_db


class ReadOnlyStore:
    """Whitelisted read surface over a Store (or any object with the same read methods).

    Mirrors `Store`'s four read methods and nothing else. Notably absent: `conn`, `upsert`,
    `update_fields`, `mark_notification_sent`, `seed_from_csv`, `close`. Closing is the
    owner's job — the chat path borrows the handle, it does not own its lifecycle.
    """

    __slots__ = ("_store",)

    def __init__(self, store):
        self._store = store

    def execute_select(self, sql: str, params=()) -> list[dict]:
        return self._store.execute_select(sql, params)

    def get(self, key: str) -> Optional[dict]:
        return self._store.get(key)

    def all(self) -> list[dict]:
        return self._store.all()

    def count(self) -> int:
        return self._store.count()

    def price_summary(self, status: str = "active") -> dict:
        """Expose the one aggregate needed by a market comparison, not listing rows."""
        return self._store.price_summary(status)

    def overall_price_summary(self) -> dict:
        """Expose the fixed all-status aggregate; no caller-controlled SQL is accepted."""
        return self._store.overall_price_summary()

    def aggregate_select(self, sql: str, params=()) -> list[dict]:
        """Expose bounded, code-built aggregate reads — see Store.aggregate_select."""
        return self._store.aggregate_select(sql, params)

    def __repr__(self) -> str:
        return f"ReadOnlyStore({self._store!r})"


class _Reader:
    """Minimal reader over a `mode=ro` connection.

    `Store.__init__` cannot be reused here: it runs `bootstrap()`, which issues CREATE TABLE
    and ALTER TABLE, and those fail immediately on a read-only connection. Decoding is
    delegated to `store._from_db` so JSON and bool columns come back in the same shape a
    normal Store would return — a divergence there would show up as chat replies that differ
    from the dashboard for the same row.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def _rows(self, sql: str, params=()) -> list[dict]:
        rows = self.conn.execute(sql, params).fetchall()
        return [{f: _from_db(f, r[f]) for f in FIELD_ORDER} for r in rows]

    def execute_select(self, sql: str, params=()) -> list[dict]:
        return self._rows(sql, params)

    def get(self, key: str) -> Optional[dict]:
        found = self._rows("SELECT * FROM listings WHERE match_key=?", (key,))
        return found[0] if found else None

    def all(self) -> list[dict]:
        return self._rows("SELECT * FROM listings")

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]

    def price_summary(self, status: str = "active") -> dict:
        row = self.conn.execute(
            "SELECT COUNT(list_price) AS listing_count, AVG(list_price) AS average_list_price, "
            "MIN(list_price) AS minimum_list_price, MAX(list_price) AS maximum_list_price "
            "FROM listings WHERE status=? AND list_price IS NOT NULL",
            (status,),
        ).fetchone()
        return dict(row)
    def overall_price_summary(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(list_price) AS listing_count, AVG(list_price) AS average_list_price "
            "FROM listings WHERE list_price IS NOT NULL"
        ).fetchone()
        return dict(row)

    def aggregate_select(self, sql: str, params=()) -> list[dict]:
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def open_readonly(db_path: str) -> ReadOnlyStore:
    """Open the pool through a connection SQLite will not let anyone write to.

    Raises sqlite3.OperationalError if the file does not exist — `mode=ro` will not create
    one, which is the intended behaviour: the chat path must never bring a pool into being.
    """
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    # check_same_thread=False for the same reason Store sets it: the bridge answers messages
    # on a background thread. Reads on a ro connection cannot corrupt each other.
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return ReadOnlyStore(_Reader(conn))
