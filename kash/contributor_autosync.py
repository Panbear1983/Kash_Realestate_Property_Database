"""Deterministic guarded sync for new contributor workspace additions only."""
from __future__ import annotations

import hashlib


class WorkspaceAutoSync:
    """Promote a first-revision workspace addition only when it cannot overwrite shared data."""

    def __init__(self, shared_store):
        self.shared_store = shared_store
        self.conn = shared_store.conn
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS contributor_workspace_sync_audit (
                sync_key TEXT PRIMARY KEY,
                workspace_key TEXT NOT NULL,
                workspace_match_key TEXT NOT NULL,
                workspace_revision INTEGER NOT NULL,
                status TEXT NOT NULL,
                code TEXT NOT NULL,
                shared_match_key TEXT,
                detail TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT
            );
            """
        )
        self.conn.commit()

    def sync_addition(self, workspace, match_key, *, workspace_key):
        record = workspace.get(workspace.contributor_id, match_key)
        sync_key = self._sync_key(workspace_key, record["match_key"], record["revision"])
        prior = self.conn.execute(
            "SELECT status,code,shared_match_key,detail FROM contributor_workspace_sync_audit WHERE sync_key=?",
            (sync_key,),
        ).fetchone()
        if prior:
            return {"status": prior[0], "code": prior[1], "shared_match_key": prior[2], "detail": prior[3]}
        verdict = self.evaluate(record)
        if not verdict["eligible"]:
            return self._record(sync_key, workspace_key, record, "held_for_review", verdict["code"], verdict["detail"])
        if self.shared_store.get(record["match_key"]) is not None:
            return self._record(sync_key, workspace_key, record, "held_for_review", "duplicate_shared_match_key",
                                "a shared listing already has this identity")
        payload = {key: value for key, value in record.items() if key not in {
            "match_key", "revision", "created_at", "updated_at", "retired_at", "deleted_at", "source_url"
        }}
        inserted = self.shared_store.insert_new_only(
            payload, source=f"contributor-autosync:{workspace_key}:{record['revision']}"
        )
        if not inserted:
            return self._record(sync_key, workspace_key, record, "held_for_review", "duplicate_shared_match_key",
                                "a shared listing already has this identity")
        return self._record(sync_key, workspace_key, record, "synced", "eligible_new_addition",
                            "new workspace listing added to shared pool", completed=True)

    def hold_workspace_change(self, record, *, workspace_key, kind):
        """Record an edit/retire/restore/delete for owner review without touching shared data."""
        if kind not in {"update", "retire", "restore", "delete"}:
            raise ValueError("unsupported workspace review kind")
        sync_key = self._sync_key(workspace_key, record["match_key"], record["revision"], kind)
        prior = self.conn.execute(
            "SELECT status,code,shared_match_key,detail FROM contributor_workspace_sync_audit WHERE sync_key=?",
            (sync_key,),
        ).fetchone()
        if prior:
            return {"status": prior[0], "code": prior[1], "shared_match_key": prior[2], "detail": prior[3]}
        return self._record(
            sync_key, workspace_key, record, "held_for_review", f"workspace_{kind}_requires_review",
            "workspace changes require owner review before shared-pool changes"
        )

    @staticmethod
    def evaluate(record):
        if record.get("deleted_at") is not None or record.get("status") != "active":
            return {"eligible": False, "code": "not_active", "detail": "only active workspace additions may sync"}
        if record.get("revision") != 1:
            return {"eligible": False, "code": "not_new", "detail": "edits require owner review"}
        if not record.get("source_url", "").startswith("https://"):
            return {"eligible": False, "code": "invalid_source", "detail": "a public HTTPS source is required"}
        if not record.get("observed_at"):
            return {"eligible": False, "code": "missing_observed_at", "detail": "an observed date is required"}
        if not record.get("match_key"):
            return {"eligible": False, "code": "missing_identity", "detail": "a stable listing identity is required"}
        return {"eligible": True, "code": "eligible_new_addition", "detail": "eligible for guarded sync"}

    def _record(self, sync_key, workspace_key, record, status, code, detail, *, completed=False):
        self.conn.execute(
            """INSERT INTO contributor_workspace_sync_audit
               (sync_key,workspace_key,workspace_match_key,workspace_revision,status,code,shared_match_key,detail,completed_at)
               VALUES(?,?,?,?,?,?,?,?,CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END)""",
            (sync_key, workspace_key, record["match_key"], record["revision"], status, code,
             record["match_key"] if status == "synced" else None, detail, completed),
        )
        self.conn.commit()
        return {"status": status, "code": code,
                "shared_match_key": record["match_key"] if status == "synced" else None, "detail": detail}

    @staticmethod
    def _sync_key(workspace_key, match_key, revision, kind="addition"):
        raw = f"{workspace_key}\0{match_key}\0{revision}\0{kind}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()
