"""Least-privilege read model for staged contributor proposals."""
from __future__ import annotations

import json

from .data_roles import DataRoles

_VISIBLE_STATUSES = frozenset({"pending", "approved", "rejected", "withdrawn", "published"})


class ReviewQueue:
    """Return proposal summaries without exposing production data or unrelated submissions."""

    def __init__(self, store, roles: DataRoles | None = None):
        self.store = store
        self.roles = roles or DataRoles(store)
        self.conn = store.conn

    def for_actor(self, actor_id, *, status: str | None = None):
        role = self.roles.role_for(actor_id)
        if status is not None and status not in _VISIBLE_STATUSES:
            raise ValueError("unsupported proposal status")
        if self.roles.can_review(actor_id):
            query = ("SELECT id,submitted_by,kind,status,payload_json,source_url,observed_at,created_at "
                     "FROM listing_proposals")
            params = []
        elif role == "contributor":
            query = ("SELECT id,submitted_by,kind,status,payload_json,source_url,observed_at,created_at "
                     "FROM listing_proposals WHERE submitted_by=?")
            params = [int(actor_id)]
        else:
            raise PermissionError("contributor or reviewer role required")
        if status is not None:
            query += " WHERE " if " WHERE " not in query else " AND "
            query += "status=?"
            params.append(status)
        query += " ORDER BY id DESC"
        return [self._summary(row) for row in self.conn.execute(query, params).fetchall()]

    @staticmethod
    def _summary(row):
        return {
            "id": int(row[0]), "submitted_by": int(row[1]), "kind": row[2], "status": row[3],
            "payload": json.loads(row[4]), "source_url": row[5], "observed_at": row[6], "created_at": row[7],
        }
