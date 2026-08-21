"""SQLite-backed listing pool: the merged source of truth.

- `bootstrap()` creates the schema from the pydantic model (one column per field)
  plus a `changelog` table.
- `seed_from_csv()` loads the existing docx-derived CSV as the first rows.
- `upsert()` merges an incoming record: it never overwrites user-protected fields,
  tracks price/status changes in the changelog, and recomputes calc fields.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date
from typing import Optional

from . import finance
from .dedup import match_key, site_from_url
from .schema import (
    BOOL_FIELDS, CALC_FIELDS, FIELD_ORDER, INT_FIELDS, JSON_FIELDS, REAL_FIELDS,
    USER_PROTECTED, Listing, sqlite_type,
)

log = logging.getLogger("kash.store")


def _to_db(field: str, value):
    if value is None:
        return None
    if field in JSON_FIELDS:
        return json.dumps(value)
    if field in BOOL_FIELDS:
        return 1 if value else 0
    return value


_ENUMS = {
    "status": {"active", "pending", "attorney_review", "off_market", "sold"},
    "view_priority": {"now", "soon", "worth", "call", "watch", "skip"},
    "property_type": {"sf_attached", "sf_semi", "sf_detached", "2fam_detached", "2fam_semi",
                      "2fam_colonial", "condo", "apartment", "lot"},
    "viewing_status": {"none", "scheduled", "seen", "skip"},
    "offer_status": {"none", "considering", "offered", "rejected", "accepted"},
}


def _coerce_fields(fields: dict) -> dict:
    """Coerce and validate values written through update_fields.

    upsert() validates via the pydantic model; update_fields does not, so a model returning
    tier "A+" or beds "3" as a string would previously land in the column verbatim. Bad enum
    values are dropped with a log line rather than written — a silently invalid status would
    make the row invisible to the alert gate.
    """
    out = {}
    for k, v in fields.items():
        if v is None:
            out[k] = None
            continue
        if k in _ENUMS and str(v) not in _ENUMS[k]:
            log.warning("update_fields: dropping invalid %s=%r", k, v)
            continue
        if k in INT_FIELDS and not isinstance(v, bool):
            try:
                out[k] = int(float(v))
            except (TypeError, ValueError):
                log.warning("update_fields: dropping non-numeric %s=%r", k, v)
            continue
        if k in REAL_FIELDS:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                log.warning("update_fields: dropping non-numeric %s=%r", k, v)
            continue
        out[k] = v
    return out


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

    def price_summary(self, status: str = "active") -> dict:
        """Return a fixed, aggregate-only list-price summary.

        This is deliberately not a general SQL escape hatch.  Market comparison callers need
        a deterministic local statistic, never listing rows, and the status stays a bound
        parameter rather than becoming interpolated SQL.
        """
        row = self.conn.execute(
            "SELECT COUNT(list_price) AS listing_count, AVG(list_price) AS average_list_price, "
            "MIN(list_price) AS minimum_list_price, MAX(list_price) AS maximum_list_price "
            "FROM listings WHERE status=? AND list_price IS NOT NULL",
            (status,),
        ).fetchone()
        return dict(row)

    def overall_price_summary(self) -> dict:
        """Return the fixed all-status list-price aggregate used by canonical chat."""
        row = self.conn.execute(
            "SELECT COUNT(list_price) AS listing_count, AVG(list_price) AS average_list_price "
            "FROM listings WHERE list_price IS NOT NULL"
        ).fetchone()
        return dict(row)

    def aggregate_select(self, sql: str, params=()) -> list[dict]:
        """Run a read-only, code-built aggregate query (COUNT/GROUP BY) and return rows
        shaped by the query's own column aliases — NOT decoded against the full listing
        schema the way execute_select is. A COUNT or GROUP BY result doesn't carry every
        FIELD_ORDER column, so execute_select's `r[f] for f in FIELD_ORDER` raises
        IndexError on it.

        Callers build `sql` from a fixed set of trusted field names (kash.query,
        kash.chat_vocabulary), never from raw user text, and every value is still bound as
        a parameter. Returns aggregate numbers/labels only — never a listing's row content
        — the same exposure as price_summary above, not the exposure of a row read.
        """
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

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

    def insert_new_only(self, incoming: dict, source: str) -> bool:
        """Atomically insert a new listing; never merge or overwrite an existing identity."""
        incoming = dict(incoming)
        key = match_key(incoming)
        if not key:
            return False
        today = date.today().isoformat()
        incoming["source"] = incoming.get("source") or source
        incoming["fetched_at"] = today
        incoming["first_seen_date"] = incoming.get("first_seen_date") or today
        incoming["last_updated"] = incoming.get("last_updated") or today
        record = finance.recompute(incoming, self.finance_cfg)
        cols = ["match_key"] + FIELD_ORDER
        vals = [key] + [_to_db(field, record.get(field)) for field in FIELD_ORDER]
        placeholders = ",".join("?" * len(cols))
        colnames = ",".join(f'"{column}"' for column in cols)
        cursor = self.conn.execute(
            f"INSERT OR IGNORE INTO listings ({colnames}) VALUES ({placeholders})", vals
        )
        if cursor.rowcount:
            self._log(key, "new_listing", f"{incoming.get('street_address')} @ {incoming.get('list_price')}", source)
        self.conn.commit()
        return bool(cursor.rowcount)

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

    def update_fields(self, key: str, fields: dict, allow_protected: bool = False) -> bool:
        """Patch specific columns on one row (used by enrichment and ranking).

        Two things this used to get wrong:

        1. It issued a bare UPDATE, so the calculated columns went stale. Only `_write`
           recomputed them, and nothing but `upsert` calls `_write` — so enriching a row with
           its property tax or estimated rent left `monthly_piti` at whatever it was before.
           Measured on the live pool: 34 of 67 rows carried a wrong PITI, the worst $545/month
           out. That is the number a buyer uses to decide what they can afford.

        2. Its docstring claimed it did not touch user-protected fields. It did — the filter
           was `k in FIELD_ORDER` only. `allow_protected` makes the promise real and forces
           the one legitimate caller (kash/rank.py, writing tier and analysis) to say so.

        Note this path bypasses pydantic entirely, so values are coerced and enum columns
        validated here rather than trusted.
        """
        fields = {k: v for k, v in fields.items() if k in FIELD_ORDER}
        if not allow_protected:
            fields = {k: v for k, v in fields.items() if k not in USER_PROTECTED}
        fields = {k: v for k, v in fields.items() if k not in CALC_FIELDS}  # derived, not set
        fields = _coerce_fields(fields)
        if not fields:
            return False
        sets = ", ".join(f'"{k}"=?' for k in fields)
        vals = [_to_db(k, v) for k, v in fields.items()] + [key]
        cur = self.conn.execute(f"UPDATE listings SET {sets} WHERE match_key=?", vals)
        if cur.rowcount:
            self._recompute_row(key)
        self.conn.commit()
        return cur.rowcount > 0

    def _recompute_row(self, key: str) -> None:
        """Refresh the derived columns from what the row now holds."""
        row = self.conn.execute(
            "SELECT * FROM listings WHERE match_key=?", (key,)).fetchone()
        if row is None:
            return
        rec = {f: _from_db(f, row[f]) for f in FIELD_ORDER if f in row.keys()}
        calc = finance.recompute(dict(rec), self.finance_cfg)
        changed = {f: calc.get(f) for f in CALC_FIELDS if calc.get(f) != rec.get(f)}
        if changed:
            sets = ", ".join(f'"{k}"=?' for k in changed)
            self.conn.execute(f"UPDATE listings SET {sets} WHERE match_key=?",
                              [_to_db(k, v) for k, v in changed.items()] + [key])

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
