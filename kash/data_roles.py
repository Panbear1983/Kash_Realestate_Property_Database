"""Narrow, auditable data-workflow roles; separate from Telegram chat access."""
from __future__ import annotations

from datetime import datetime, timezone

ROLE_CONTRIBUTOR = "contributor"
ROLE_REVIEWER = "reviewer"
ROLE_PUBLISHER = "publisher"
ROLE_OWNER = "owner"
ROLES = frozenset({ROLE_CONTRIBUTOR, ROLE_REVIEWER, ROLE_PUBLISHER, ROLE_OWNER})


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class DataRoles:
    """Application-only role registry. It does not grant database or shell access."""

    def __init__(self, store):
        self.conn = store.conn
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS data_role_assignments (
                telegram_user_id INTEGER PRIMARY KEY,
                role TEXT NOT NULL,
                granted_by INTEGER NOT NULL,
                granted_at TEXT NOT NULL,
                revoked_at TEXT,
                reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS data_role_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                role TEXT,
                granted_by INTEGER NOT NULL,
                reason TEXT NOT NULL,
                ts TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def role_for(self, user_id):
        row = self.conn.execute(
            "SELECT role FROM data_role_assignments WHERE telegram_user_id=? AND revoked_at IS NULL",
            (int(user_id),),
        ).fetchone()
        return row[0] if row else None

    def bootstrap_owner(self, user_id):
        """Initialize exactly one owner in a fresh role registry, for local owner setup only."""
        existing = self.conn.execute(
            "SELECT COUNT(*) FROM data_role_assignments WHERE role=? AND revoked_at IS NULL",
            (ROLE_OWNER,),
        ).fetchone()[0]
        if existing:
            raise PermissionError("an owner is already assigned")
        self._assign(int(user_id), ROLE_OWNER, int(user_id), "initial owner setup", "bootstrap")

    def grant(self, actor_id, subject_id, role, *, reason):
        if self.role_for(actor_id) != ROLE_OWNER:
            raise PermissionError("only owner may grant data roles")
        if role not in ROLES or role == ROLE_OWNER:
            raise ValueError("only narrow non-owner roles may be granted")
        self._assign(int(subject_id), role, int(actor_id), reason, "grant")

    def revoke(self, actor_id, subject_id, *, reason):
        if self.role_for(actor_id) != ROLE_OWNER:
            raise PermissionError("only owner may revoke data roles")
        subject_id = int(subject_id)
        current = self.role_for(subject_id)
        if current is None:
            return False
        if current == ROLE_OWNER:
            raise PermissionError("owner role cannot be revoked by this workflow")
        now = _now()
        self.conn.execute("UPDATE data_role_assignments SET revoked_at=? WHERE telegram_user_id=?", (now, subject_id))
        self.conn.execute(
            "INSERT INTO data_role_audit(telegram_user_id,event,role,granted_by,reason,ts) VALUES(?,?,?,?,?,?)",
            (subject_id, "revoke", current, int(actor_id), self._reason(reason), now),
        )
        self.conn.commit()
        return True

    def can_submit(self, user_id) -> bool:
        return self.role_for(user_id) in {ROLE_CONTRIBUTOR, ROLE_REVIEWER, ROLE_PUBLISHER, ROLE_OWNER}

    def can_review(self, user_id) -> bool:
        return self.role_for(user_id) in {ROLE_REVIEWER, ROLE_OWNER}

    def can_publish(self, user_id) -> bool:
        return self.role_for(user_id) in {ROLE_PUBLISHER, ROLE_OWNER}

    def audit_for(self, user_id):
        rows = self.conn.execute(
            "SELECT telegram_user_id,event,role,granted_by,reason,ts FROM data_role_audit WHERE telegram_user_id=? ORDER BY id",
            (int(user_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def _assign(self, subject_id, role, actor_id, reason, event):
        now = _now()
        reason = self._reason(reason)
        self.conn.execute(
            """INSERT INTO data_role_assignments(telegram_user_id,role,granted_by,granted_at,revoked_at,reason)
               VALUES(?,?,?,?,NULL,?)
               ON CONFLICT(telegram_user_id) DO UPDATE SET role=excluded.role, granted_by=excluded.granted_by,
                   granted_at=excluded.granted_at, revoked_at=NULL, reason=excluded.reason""",
            (subject_id, role, actor_id, now, reason),
        )
        self.conn.execute(
            "INSERT INTO data_role_audit(telegram_user_id,event,role,granted_by,reason,ts) VALUES(?,?,?,?,?,?)",
            (subject_id, event, role, actor_id, reason, now),
        )
        self.conn.commit()

    @staticmethod
    def _reason(value):
        text = str(value or "").strip()
        if not text or len(text) > 240:
            raise ValueError("reason must be 1–240 characters")
        return text
