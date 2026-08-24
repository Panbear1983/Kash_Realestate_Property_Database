"""Isolated, structured, expiring short-term context for Kash private chats."""
from __future__ import annotations

import json
import os
import pwd
import re
import sqlite3
import time
from pathlib import Path

from .chat_policy import CHAT_FIELDS, MAX_OR_VALUES, OP_ALIAS

_CANONICAL_OPS = frozenset(OP_ALIAS.values())
_MAX_STORED_FILTERS = 8   # bounds prompt growth from replayed context

#: A pending clarification is deliberately shorter-lived than the session row it rides on —
#: mirrors chat._FOLLOWUP_CONTEXT_TTL_SECONDS: a question asked five minutes ago is stale.
PENDING_TTL_SECONDS = 300
_MAX_QUESTION_CHARS = 200
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_question(text) -> str:
    """The stored question is MODEL-authored clarify text (never the user's message), but
    it still round-trips from disk into a future prompt, so it is bounded and stripped the
    same way twice — once on save, again on load."""
    return _CONTROL.sub("", str(text or "")).strip()[:_MAX_QUESTION_CHARS]


def _clean_pending(raw) -> dict | None:
    """Re-validate a pending payload loaded from disk — replayed data is untrusted, same
    reasoning as _clean_stored_filter."""
    if not isinstance(raw, dict):
        return None
    filters_raw = raw.get("filters")
    if not isinstance(filters_raw, list):
        return None
    cleaned = [_clean_stored_filter(f) for f in filters_raw]
    filters = [f for f in cleaned if f is not None][:_MAX_STORED_FILTERS]
    if not filters:
        return None                       # a pending with no partial filters is pointless
    sort = str(raw.get("sort") or "")
    if sort not in CHAT_FIELDS:
        sort = "rank"
    order = "desc" if str(raw.get("order") or "").lower().startswith("d") else "asc"
    try:
        limit = int(raw.get("limit"))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 100))
    return {"filters": filters, "sort": sort, "order": order, "limit": limit,
            "question": _clean_question(raw.get("question"))}


def _clean_stored_filter(raw) -> dict | None:
    """Re-validate the shape and re-apply the CHAT_FIELDS ceiling here, since whatever is
    stored is replayed verbatim into a FUTURE routing prompt regardless of that future
    message's own access level — an owner's private-field filter must never round-trip
    back into a prompt that is always built at 'read' level (see chat.py's
    _visible_columns)."""
    if not isinstance(raw, dict):
        return None
    field, op = str(raw.get("field") or ""), str(raw.get("op") or "")
    if field not in CHAT_FIELDS or op not in _CANONICAL_OPS:
        return None
    values = raw.get("values")
    if values:
        # An OR group (see chat_policy._clean_filter): scalar alternatives for one field,
        # only ever stored for = / contains. Anything malformed drops the whole filter.
        if not isinstance(values, (list, tuple)) or op not in ("=", "contains"):
            return None
        if any(v is None or isinstance(v, (list, dict, tuple, set)) for v in values):
            return None
        cleaned = [str(v) for v in values][:MAX_OR_VALUES]
        if not cleaned:
            return None
        return {"field": field, "op": op, "value": "", "values": cleaned}
    value = raw.get("value")
    if value is None or isinstance(value, (list, dict, tuple, set)):
        return None
    return {"field": field, "op": op, "value": str(value)}


def default_path() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "state" / "kash" / "sessions.sqlite"


class SessionStore:
    def __init__(self, path=None, *, ttl_seconds=1800):
        self.path = str(path or default_path())
        self.ttl_seconds = int(ttl_seconds)

    def _connect(self):
        path = Path(self.path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try: path.parent.chmod(0o700)
        except OSError: pass
        con = sqlite3.connect(self.path)
        con.execute("CREATE TABLE IF NOT EXISTS private_sessions (user_id INTEGER PRIMARY KEY, filters_json TEXT NOT NULL, listing_keys_json TEXT NOT NULL, selected_key TEXT, expires_at INTEGER NOT NULL)")
        # Defensive column migration, same style as store._migrate_columns: a pre-pending
        # database gains the columns in place; an already-migrated one raises and is left
        # alone. Old code SELECTs explicit columns, so a downgrade simply ignores these.
        for ddl in ("ALTER TABLE private_sessions ADD COLUMN pending_json TEXT",
                    "ALTER TABLE private_sessions ADD COLUMN pending_expires_at INTEGER"):
            try:
                con.execute(ddl)
            except sqlite3.OperationalError:
                pass
        try: os.chmod(self.path, 0o600)
        except OSError: pass
        return con

    def save(self, user_id, *, filters, listing_keys, selected_key=None, raw_text=None, now=None):
        """Persist only allow-listed structured search state; raw text is deliberately
        ignored. `filters` is the real SafeSpec shape — a list of {field,op,value} dicts —
        re-validated here against CHAT_FIELDS regardless of the caller's own access level
        (see _clean_stored_filter)."""
        del raw_text
        current = int(time.time()) if now is None else int(now)
        cleaned = [_clean_stored_filter(f) for f in (filters or [])]
        safe_filters = [f for f in cleaned if f is not None][:_MAX_STORED_FILTERS]
        safe_keys = tuple(str(key) for key in listing_keys if str(key))
        selected = str(selected_key) if selected_key in safe_keys else None
        with self._connect() as con:
            con.execute("INSERT INTO private_sessions(user_id,filters_json,listing_keys_json,selected_key,expires_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET filters_json=excluded.filters_json, listing_keys_json=excluded.listing_keys_json, selected_key=excluded.selected_key, expires_at=excluded.expires_at", (int(user_id), json.dumps(safe_filters, sort_keys=True), json.dumps(safe_keys), selected, current + self.ttl_seconds))

    def save_search(self, user_id, *, filters, rows, now=None):
        """Persist the real filter list AND the returned match keys from a successful safe
        search; never row content. Retaining the filters (not just sort) is what lets a
        later message modify rather than only reference the last search."""
        keys = [row.get("property_id") or row.get("source_url") or row.get("listing_url")
                or row.get("match_key")
                for row in rows if isinstance(row, dict)
                and (row.get("property_id") or row.get("source_url") or row.get("listing_url")
                     or row.get("match_key"))]
        self.save(user_id, filters=filters, listing_keys=keys, now=now)

    def load(self, user_id, *, now=None):
        current = int(time.time()) if now is None else int(now)
        with self._connect() as con:
            row = con.execute("SELECT filters_json,listing_keys_json,selected_key,expires_at FROM private_sessions WHERE user_id=?", (int(user_id),)).fetchone()
            if not row or row[3] <= current:
                con.execute("DELETE FROM private_sessions WHERE user_id=?", (int(user_id),))
                return None
        filters = json.loads(row[0])
        if not isinstance(filters, list):
            filters = []          # a pre-migration dict-shaped row; ignore rather than crash
        return {"filters": filters, "listing_keys": tuple(json.loads(row[1])),
                "selected_key": row[2], "expires_at": row[3]}

    def save_pending(self, user_id, *, filters, sort="rank", order="asc", limit=20,
                     question="", now=None):
        """Persist a partial search awaiting one clarifying answer. Stores only the same
        allow-listed filter shape save() does, plus the MODEL-authored question text —
        never the user's message. Rides the session row; the session's own expires_at is
        extended to at least the pending's so an about-to-expire row cannot take a live
        pending down with it."""
        current = int(time.time()) if now is None else int(now)
        cleaned = [_clean_stored_filter(f) for f in (filters or [])]
        safe_filters = [f for f in cleaned if f is not None][:_MAX_STORED_FILTERS]
        if not safe_filters:
            return                        # nothing understood yet — nothing worth holding
        payload = json.dumps({
            "filters": safe_filters,
            "sort": str(sort or "rank"),
            "order": str(order or "asc"),
            "limit": int(limit) if isinstance(limit, (int, float)) else 20,
            "question": _clean_question(question),
        }, sort_keys=True)
        pending_expires = current + PENDING_TTL_SECONDS
        with self._connect() as con:
            con.execute(
                "INSERT INTO private_sessions(user_id,filters_json,listing_keys_json,"
                "selected_key,expires_at,pending_json,pending_expires_at)"
                " VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET"
                " pending_json=excluded.pending_json,"
                " pending_expires_at=excluded.pending_expires_at,"
                " expires_at=MAX(private_sessions.expires_at, excluded.expires_at)",
                (int(user_id), "[]", "[]", None, pending_expires, payload, pending_expires))

    def consume_pending(self, user_id, *, now=None):
        """Atomic read-and-delete: a pending is answered (or abandoned) exactly once, so a
        wrong merge can never compound across turns. Returns the re-validated payload dict
        or None."""
        current = int(time.time()) if now is None else int(now)
        with self._connect() as con:
            row = con.execute(
                "SELECT pending_json, pending_expires_at FROM private_sessions"
                " WHERE user_id=?", (int(user_id),)).fetchone()
            if not row or not row[0]:
                return None
            con.execute(
                "UPDATE private_sessions SET pending_json=NULL, pending_expires_at=NULL"
                " WHERE user_id=?", (int(user_id),))
        if not isinstance(row[1], (int, float)) or row[1] <= current:
            return None
        try:
            raw = json.loads(row[0])
        except ValueError:
            return None
        return _clean_pending(raw)

    def clear(self, user_id):
        with self._connect() as con:
            return con.execute("DELETE FROM private_sessions WHERE user_id=?", (int(user_id),)).rowcount > 0
