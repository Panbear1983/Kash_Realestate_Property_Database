"""SQLite-backed listing pool: the merged source of truth.

- `bootstrap()` creates the schema from the pydantic model (one column per field)
  plus a `changelog` table.
- `seed_from_csv()` loads the existing docx-derived CSV as the first rows.
- `upsert()` merges an incoming record: it never overwrites user-protected fields,
  tracks price/status changes in the changelog, and recomputes calc fields.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Optional

from . import finance
from .dedup import match_key, site_from_url
from .schema import (
    BOOL_FIELDS, FIELD_ORDER, JSON_FIELDS, USER_PROTECTED, Listing, sqlite_type,
)


def _to_db(field: str, value):
    if value is None:
        return None
    if field in JSON_FIELDS:
        return json.dumps(value)
    if field in BOOL_FIELDS:
        return 1 if value else 0
    return value


def _from_db(field: str, value):
    if value is None:
        return None
    if field in JSON_FIELDS:
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None
    if field in BOOL_FIELDS:
        return bool(value)
    return value


class Store:
    def __init__(self, db_path: str, finance_cfg: Optional[dict] = None):
        # check_same_thread=False: the TUI 'ask' worker and the Telegram bot both touch the
        # store from a background thread. Writes are no longer single-threaded either — usage
        # metering records on the chat path, and the 07:00 update job writes concurrently.
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.db_path = db_path
        self.conn.row_factory = sqlite3.Row
        # WAL lets readers proceed during a write instead of blocking, and busy_timeout turns
        # an immediate "database is locked" into a short wait. Without both, a chat question
        # landing during the nightly run would raise rather than queue. Guarded because WAL is
        # unavailable on some filesystems (network mounts), where the rollback journal still
        # works correctly, just with coarser locking.
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.finance_cfg = finance_cfg or {}
        self.bootstrap()

    def bootstrap(self):
        cols = ",\n  ".join(f'"{f}" {sqlite_type(f)}' for f in FIELD_ORDER)
        self.conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS listings (
              match_key TEXT PRIMARY KEY,
              {cols}
            );
            CREATE TABLE IF NOT EXISTS changelog (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              match_key TEXT,
              event TEXT,
              detail TEXT,
              source TEXT,
              ts TEXT
            );
            CREATE TABLE IF NOT EXISTS notification_delivery (
              recipient_id INTEGER,
              kind TEXT,
              ts TEXT,
              PRIMARY KEY (recipient_id, kind)
            );
            """
        )
        self._migrate_columns()
        self.conn.commit()

    def _migrate_columns(self):
        """Add any schema field missing from an existing `listings` table.

        CREATE TABLE IF NOT EXISTS above is a no-op once the table exists, so a new field on
        the Listing model would otherwise never reach a live pool.db. Same defensive shape as
        kash/access.py's custom_greeting migration. Additive only — never drops or rewrites a
        column, so user-owned values (analysis, my_notes, tier) are untouched.
        """
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(listings)")}
        if not existing:
            return
        for field in FIELD_ORDER:
            if field not in existing:
                self.conn.execute(
                    f'ALTER TABLE listings ADD COLUMN "{field}" {sqlite_type(field)}'
                )

    # --- reads ---
    def get(self, key: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM listings WHERE match_key=?", (key,)
        ).fetchone()
        if not row:
            return None
        return {f: _from_db(f, row[f]) for f in FIELD_ORDER}

    def all(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM listings").fetchall()
        return [{f: _from_db(f, r[f]) for f in FIELD_ORDER} for r in rows]

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]

    def execute_select(self, sql: str, params=()) -> list[dict]:
        """Run a SELECT * built by kash.query and decode rows to schema dicts."""
        rows = self.conn.execute(sql, params).fetchall()
        return [{f: _from_db(f, r[f]) for f in FIELD_ORDER} for r in rows]

    def recent_changes(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM changelog ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def notification_sent(self, recipient_id: int, kind: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM notification_delivery WHERE recipient_id=? AND kind=?",
            (int(recipient_id), kind),
        ).fetchone() is not None

    def mark_notification_sent(self, recipient_id: int, kind: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO notification_delivery (recipient_id,kind,ts) VALUES (?,?,?)",
            (int(recipient_id), kind, date.today().isoformat()),
        )
        self.conn.commit()

    # --- writes ---
    def _log(self, key: str, event: str, detail: str, source: str):
        self.conn.execute(
            "INSERT INTO changelog (match_key,event,detail,source,ts) VALUES (?,?,?,?,?)",
            (key, event, detail, source, date.today().isoformat()),
        )

    def _write(self, key: str, rec: dict):
        rec = finance.recompute(rec, self.finance_cfg)
        cols = ["match_key"] + FIELD_ORDER
        vals = [key] + [_to_db(f, rec.get(f)) for f in FIELD_ORDER]
        placeholders = ",".join("?" * len(cols))
        colnames = ",".join(f'"{c}"' for c in cols)
        self.conn.execute(
            f"INSERT OR REPLACE INTO listings ({colnames}) VALUES ({placeholders})",
            vals,
        )

    def upsert(self, incoming: dict, source: str) -> str:
        """Merge one incoming (already-validated) record. Returns the outcome:
        'inserted' | 'updated' | 'unchanged'."""
        incoming = dict(incoming)
        key = match_key(incoming)
        if not key:
            return "rejected"
        today = date.today().isoformat()
        incoming["source"] = incoming.get("source") or source
        incoming["fetched_at"] = today

        existing = self.get(key)
        if existing is None:
            # explicit (not setdefault): model_dump() includes these keys as None, so
            # setdefault would be a no-op and the dates would never populate.
            if not incoming.get("first_seen_date"):
                incoming["first_seen_date"] = today
            if not incoming.get("last_updated"):
                incoming["last_updated"] = today
            self._write(key, incoming)
            self._log(key, "new_listing",
                      f"{incoming.get('street_address')} @ {incoming.get('list_price')}", source)
            self.conn.commit()
            return "inserted"

        merged = dict(existing)
        changed = False

        # Track price + status transitions before merging.
        old_price = existing.get("list_price")
        new_price = incoming.get("list_price")
        if new_price and old_price and new_price != old_price:
            ev = "price_drop" if new_price < old_price else "price_increase"
            self._log(key, ev, f"{old_price} -> {new_price}", source)
            hist = existing.get("price_history") or []
            hist.append({"date": today, "price": new_price})
            merged["price_history"] = hist
            if not existing.get("original_list_price"):
                merged["original_list_price"] = old_price
            changed = True

        old_status = existing.get("status")
        new_status = incoming.get("status")
        if new_status and new_status != old_status:
            self._log(key, "status_change", f"{old_status} -> {new_status}", source)
            if old_status in ("pending", "off_market") and new_status == "active":
                merged["times_relisted"] = (existing.get("times_relisted") or 0) + 1
            changed = True

        # Merge sourced fields; skip user-protected, skip None (no clobber with blanks).
        for f in FIELD_ORDER:
            if f in USER_PROTECTED:
                continue
            v = incoming.get(f)
            if v is None:
                continue
            if merged.get(f) != v:
                merged[f] = v
                changed = True

        merged["last_updated"] = today
        merged["fetched_at"] = today
        self._write(key, merged)
        self.conn.commit()
        return "updated" if changed else "unchanged"

    def update_fields(self, key: str, fields: dict) -> bool:
        """Patch specific columns on one row (used by enrichment). Does not merge,
        clobber `source`, or touch user-protected fields."""
        fields = {k: v for k, v in fields.items() if k in FIELD_ORDER}
        if not fields:
            return False
        sets = ", ".join(f'"{k}"=?' for k in fields)
        vals = [_to_db(k, v) for k, v in fields.items()] + [key]
        cur = self.conn.execute(
            f"UPDATE listings SET {sets} WHERE match_key=?", vals
        )
        self.conn.commit()
        return cur.rowcount > 0

    def seed_from_csv(self, csv_path: str) -> int:
        """Load the docx-derived CSV as the initial pool (idempotent by match_key)."""
        import csv
        n = 0
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                listing = Listing(**row)
                rec = listing.model_dump()
                src = site_from_url(rec.get("listing_url")) or "seed:docx"
                rec["source"] = src
                self.upsert(rec, source=src)
                n += 1
        return n

    def close(self):
        self.conn.close()
