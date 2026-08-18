"""Owner-managed registry for isolated contributor workspace databases."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from .data_roles import ROLE_CONTRIBUTOR, ROLE_OWNER


class ContributorWorkspaces:
    """Map a granted contributor to one opaque, application-controlled workspace path."""

    def __init__(self, store, roles, *, root):
        self.store = store
        self.roles = roles
        self.conn = store.conn
        self.root = Path(root).expanduser().resolve()
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS contributor_workspaces (
                contributor_id INTEGER PRIMARY KEY,
                workspace_key TEXT UNIQUE NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                auto_sync_additions INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                disabled_at TEXT,
                disabled_by INTEGER,
                reason TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def create(self, actor_id, contributor_id, *, reason):
        self._require_owner(actor_id)
        if self.roles.role_for(contributor_id) != ROLE_CONTRIBUTOR:
            raise PermissionError("workspace requires an active contributor role")
        existing = self._row(contributor_id)
        if existing:
            return self._record(existing)
        key = secrets.token_urlsafe(18)
        self.conn.execute(
            """INSERT INTO contributor_workspaces
               (contributor_id, workspace_key, enabled, auto_sync_additions, created_by, reason)
               VALUES(?,?,1,1,?,?)""",
            (int(contributor_id), key, int(actor_id), self._reason(reason)),
        )
        self.conn.commit()
        self._ensure_path(key)
        return self._record(self._row(contributor_id))

    def disable(self, actor_id, contributor_id, *, reason):
        self._require_owner(actor_id)
        row = self._row(contributor_id)
        if not row:
            raise ValueError("contributor workspace does not exist")
        self.conn.execute(
            """UPDATE contributor_workspaces
               SET enabled=0, disabled_at=CURRENT_TIMESTAMP, disabled_by=?, reason=?
               WHERE contributor_id=?""",
            (int(actor_id), self._reason(reason), int(contributor_id)),
        )
        self.conn.commit()
        return self._record(self._row(contributor_id))

    def set_auto_sync(self, actor_id, contributor_id, *, enabled, reason):
        self._require_owner(actor_id)
        if not isinstance(enabled, bool):
            raise ValueError("auto-sync setting must be boolean")
        row = self._row(contributor_id)
        if not row:
            raise ValueError("contributor workspace does not exist")
        self.conn.execute(
            "UPDATE contributor_workspaces SET auto_sync_additions=?, reason=? WHERE contributor_id=?",
            (1 if enabled else 0, self._reason(reason), int(contributor_id)),
        )
        self.conn.commit()
        return self._record(self._row(contributor_id))

    def for_contributor(self, contributor_id, *, require_enabled=True):
        if self.roles.role_for(contributor_id) != ROLE_CONTRIBUTOR:
            raise PermissionError("active contributor role required")
        row = self._row(contributor_id)
        if not row or (require_enabled and not bool(row[2])):
            raise PermissionError("no active contributor workspace")
        return self._record(row)

    def _row(self, contributor_id):
        return self.conn.execute(
            """SELECT contributor_id, workspace_key, enabled, auto_sync_additions,
                      created_by, created_at, disabled_at, disabled_by, reason
               FROM contributor_workspaces WHERE contributor_id=?""",
            (int(contributor_id),),
        ).fetchone()

    def _record(self, row):
        return {
            "contributor_id": int(row[0]), "workspace_key": row[1], "enabled": bool(row[2]),
            "auto_sync_additions": bool(row[3]), "created_by": int(row[4]),
            "created_at": row[5], "disabled_at": row[6], "disabled_by": row[7], "reason": row[8],
            "path": self._path(row[1]),
        }

    def _ensure_path(self, key):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        path = self._path(key)
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        return path

    def _path(self, key):
        if not key or "/" in key or "\\" in key or ".." in key:
            raise ValueError("invalid workspace key")
        return self.root / f"{key}.sqlite3"

    def _require_owner(self, actor_id):
        if self.roles.role_for(actor_id) != ROLE_OWNER:
            raise PermissionError("owner role required")

    @staticmethod
    def _reason(value):
        text = str(value or "").strip()
        if not text or len(text) > 240:
            raise ValueError("reason must be 1–240 characters")
        return text
