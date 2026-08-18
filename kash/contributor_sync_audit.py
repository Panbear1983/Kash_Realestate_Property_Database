"""Owner-only, least-privilege read model for contributor workspace sync audit records."""
from __future__ import annotations

from .data_roles import ROLE_OWNER, DataRoles


class WorkspaceSyncAuditReadModel:
    """Expose sync outcomes without leaking sync keys, workspace paths, or internal detail."""

    def __init__(self, store, roles: DataRoles | None = None):
        self.conn = store.conn
        self.roles = roles or DataRoles(store)

    def for_owner(self, actor_id):
        if self.roles.role_for(actor_id) != ROLE_OWNER:
            raise PermissionError("owner data role required to view workspace sync audit")
        try:
            rows = self.conn.execute(
                """SELECT workspace_match_key, workspace_revision, status, code, shared_match_key
                   FROM contributor_workspace_sync_audit
                   ORDER BY created_at DESC, sync_key DESC"""
            ).fetchall()
        except Exception as exc:
            # A dashboard may open before the first guarded sync has initialized its audit table.
            if "no such table" in str(exc).lower():
                return []
            raise
        return [self._summary(row) for row in rows]

    @staticmethod
    def _summary(row):
        workspace_key, revision, status, code, shared_key = row
        return {
            "listing_key": shared_key or workspace_key,
            "status": status,
            "reason": code,
            "provenance": f"workspace revision {revision}",
        }
