"""Isolated, structured, expiring short-term context for Kash private chats."""
from __future__ import annotations

import json
import os
import pwd
import sqlite3
import time
from pathlib import Path


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
        try: os.chmod(self.path, 0o600)
        except OSError: pass
        return con

    def save(self, user_id, *, filters, listing_keys, selected_key=None, raw_text=None, now=None):
        """Persist only allow-listed structured search state; raw text is deliberately ignored."""
        del raw_text
        current = int(time.time()) if now is None else int(now)
        safe_filters = {str(k): v for k, v in dict(filters).items()
                        if str(k) in {"status", "max_price", "min_price", "neighborhood", "property_type", "min_baths", "sort"}}
        safe_keys = tuple(str(key) for key in listing_keys if str(key))
        selected = str(selected_key) if selected_key in safe_keys else None
        with self._connect() as con:
            con.execute("INSERT INTO private_sessions(user_id,filters_json,listing_keys_json,selected_key,expires_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET filters_json=excluded.filters_json, listing_keys_json=excluded.listing_keys_json, selected_key=excluded.selected_key, expires_at=excluded.expires_at", (int(user_id), json.dumps(safe_filters, sort_keys=True), json.dumps(safe_keys), selected, current + self.ttl_seconds))

    def save_search(self, user_id, *, filters, rows, now=None):
        """Persist only returned match keys from a successful safe search; never row content."""
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
        return {"filters": json.loads(row[0]), "listing_keys": tuple(json.loads(row[1])), "selected_key": row[2], "expires_at": row[3]}

    def clear(self, user_id):
        with self._connect() as con:
            return con.execute("DELETE FROM private_sessions WHERE user_id=?", (int(user_id),)).rowcount > 0
