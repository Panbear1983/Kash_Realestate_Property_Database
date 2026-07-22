"""Telegram bot access control — who may chat with the read-only Kash data bot.

Stored as an `access` table in pool.db so the dashboard (which manages it) and the Telegram
bridge (which checks it per message) share one source of truth — approvals take effect
without restarting the bridge. Everyone `allowed` gets read-only query access, the only
thing the bot can do.

Statuses: 'pending' (asked, awaiting approval) · 'allowed' (approved) · 'denied'.
"""
from __future__ import annotations

from datetime import date

_COLS = ["telegram_user_id", "name", "status", "access_level", "first_message", "updated_at"]


class Access:
    def __init__(self, store):
        self.conn = store.conn
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS access (
                telegram_user_id INTEGER PRIMARY KEY,
                name TEXT,
                status TEXT DEFAULT 'pending',
                access_level TEXT DEFAULT 'read',
                first_message TEXT,
                updated_at TEXT
            )"""
        )
        self.conn.commit()

    def is_allowed(self, user_id) -> bool:
        row = self.conn.execute(
            "SELECT status FROM access WHERE telegram_user_id=?", (user_id,)
        ).fetchone()
        return bool(row and row[0] == "allowed")

    def ensure_owner(self, user_ids) -> None:
        for uid in user_ids or []:
            self.conn.execute(
                """INSERT INTO access (telegram_user_id, name, status, access_level, updated_at)
                   VALUES (?, 'owner', 'allowed', 'read', ?)
                   ON CONFLICT(telegram_user_id) DO UPDATE SET status='allowed'""",
                (int(uid), date.today().isoformat()),
            )
        self.conn.commit()

    def request(self, user_id, name, message) -> str:
        """Record a newcomer's access request. Returns the resulting status."""
        row = self.conn.execute(
            "SELECT status FROM access WHERE telegram_user_id=?", (user_id,)
        ).fetchone()
        if row:
            return row[0]                       # already known (pending/allowed/denied)
        self.conn.execute(
            """INSERT INTO access (telegram_user_id, name, status, access_level, first_message, updated_at)
               VALUES (?, ?, 'pending', 'read', ?, ?)""",
            (user_id, name, (message or "")[:200], date.today().isoformat()),
        )
        self.conn.commit()
        return "pending"

    def add(self, user_id, name=None, status="allowed", first_message=None) -> None:
        """Pre-authorize (or update) a user by Telegram ID — no prior message needed."""
        self.conn.execute(
            """INSERT INTO access
                   (telegram_user_id, name, status, access_level, first_message, updated_at)
               VALUES (?, ?, ?, 'read', ?, ?)
               ON CONFLICT(telegram_user_id) DO UPDATE SET
                   status=excluded.status,
                   name=COALESCE(excluded.name, access.name),
                   first_message=COALESCE(excluded.first_message, access.first_message),
                   updated_at=excluded.updated_at""",
            (int(user_id), name, status,
             None if first_message is None else str(first_message)[:200],
             date.today().isoformat()),
        )
        self.conn.commit()

    def edit(self, user_id, name=None, status=None, access_level=None,
             first_message=None) -> None:
        """Update any subset of an existing entry's editable fields."""
        sets, vals = [], []
        if name is not None:
            sets.append("name=?"); vals.append(name)
        if status is not None:
            sets.append("status=?"); vals.append(status)
        if access_level is not None:
            sets.append("access_level=?"); vals.append(access_level)
        if first_message is not None:
            sets.append("first_message=?"); vals.append(str(first_message)[:200])
        if not sets:
            return
        sets.append("updated_at=?"); vals.append(date.today().isoformat())
        vals.append(int(user_id))
        self.conn.execute(
            f"UPDATE access SET {', '.join(sets)} WHERE telegram_user_id=?", vals
        )
        self.conn.commit()

    def set_status(self, user_id, status) -> None:
        self.conn.execute(
            "UPDATE access SET status=?, updated_at=? WHERE telegram_user_id=?",
            (status, date.today().isoformat(), user_id),
        )
        self.conn.commit()

    def remove(self, user_id) -> None:
        self.conn.execute("DELETE FROM access WHERE telegram_user_id=?", (user_id,))
        self.conn.commit()

    def all(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT telegram_user_id, name, status, access_level, first_message, updated_at
               FROM access
               ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'allowed' THEN 1 ELSE 2 END,
                        updated_at DESC"""
        ).fetchall()
        return [dict(zip(_COLS, r)) for r in rows]
