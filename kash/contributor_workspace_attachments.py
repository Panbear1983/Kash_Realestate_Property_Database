"""Quarantined evidence for active contributor workspace listings.

Attachments are opaque files outside the workspace database. This module never extracts
content and never modifies workspace listing or sync payloads.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from .contribution_attachments import (
    ATTACHMENT_RETENTION_DAYS,
    MAX_ATTACHMENT_BYTES,
    _MAGIC,
    _MANUAL_ONLY,
)


class WorkspaceAttachmentService:
    """Store bounded evidence only for the owning contributor's active workspace record."""

    def __init__(self, workspace, *, quarantine_dir):
        self.workspace = workspace
        self.conn = workspace.conn
        self.quarantine_dir = Path(quarantine_dir).expanduser()
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS workspace_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_match_key TEXT NOT NULL,
                uploaded_by INTEGER NOT NULL,
                stored_name TEXT UNIQUE NOT NULL,
                sha256 TEXT NOT NULL,
                media_kind TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                status TEXT NOT NULL,
                extraction TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        self.conn.commit()

    def stage_mine(self, actor_id, workspace_match_key, filename, content: bytes):
        """Quarantine one owner file; a missing listing key is permitted only for a draft input."""
        if workspace_match_key:
            listing = self.workspace.get(actor_id, workspace_match_key)
            if listing["status"] != "active" or listing["deleted_at"] is not None:
                raise ValueError("attachments may be added only to an active workspace listing")
        else:
            self.workspace._require_owner(actor_id)
        if not isinstance(content, bytes) or not content:
            raise ValueError("attachment content must be non-empty bytes")
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment exceeds 10 MiB limit")
        suffix = Path(str(filename)).suffix.lower()
        magic = _MAGIC.get(suffix)
        if not magic or not content.startswith(magic):
            raise ValueError("attachment type is not allowed or content does not match its extension")

        self.quarantine_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.quarantine_dir.chmod(0o700)
        except OSError:
            pass
        stored_name = f"{uuid.uuid4().hex}{suffix}"
        stored_match_key = str(workspace_match_key) if workspace_match_key else ""
        final_path = self.quarantine_dir / stored_name
        temp_path = self.quarantine_dir / f".{stored_name}.tmp"
        try:
            with open(temp_path, "xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, final_path)
            digest = hashlib.sha256(content).hexdigest()
            status = "manual_review" if suffix in _MANUAL_ONLY else "quarantined"
            self.conn.execute(
                """INSERT INTO workspace_attachments
                   (workspace_match_key,uploaded_by,stored_name,sha256,media_kind,byte_size,status,extraction)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (stored_match_key, int(actor_id), stored_name, digest, suffix[1:], len(content), status, "disabled"),
            )
            self.conn.commit()
        except Exception:
            temp_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise
        return {
            "id": int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]),
            "workspace_match_key": str(workspace_match_key) if workspace_match_key else None, "uploaded_by": int(actor_id),
            "sha256": digest, "media_kind": suffix[1:], "byte_size": len(content),
            "status": status, "extraction": "disabled", "path": str(final_path),
        }

    def purge_expired(self) -> int:
        """Delete retained bytes after 30 days while retaining the audit row."""
        rows = self.conn.execute(
            """SELECT id, stored_name FROM workspace_attachments
               WHERE status IN ('quarantined','manual_review')
                 AND created_at < datetime('now', ?)""",
            (f"-{ATTACHMENT_RETENTION_DAYS} days",),
        ).fetchall()
        for attachment_id, stored_name in rows:
            (self.quarantine_dir / stored_name).unlink(missing_ok=True)
            self.conn.execute("UPDATE workspace_attachments SET status='expired' WHERE id=?", (attachment_id,))
        self.conn.commit()
        return len(rows)

    def document_for_mine(self, actor_id, attachment_id):
        """Resolve an owned, still-quarantined document for internal local extraction."""
        self.workspace._require_owner(actor_id)
        row = self.conn.execute(
            """SELECT stored_name,media_kind FROM workspace_attachments
               WHERE id=? AND uploaded_by=? AND media_kind IN ('doc','docx')
                 AND status IN ('quarantined','manual_review')""",
            (int(attachment_id), int(actor_id)),
        ).fetchone()
        if not row:
            raise ValueError("document attachment is unavailable")
        path = self.quarantine_dir / row[0]
        if not path.is_file():
            raise ValueError("document attachment is unavailable")
        return {"id": int(attachment_id), "media_kind": row[1], "path": path}
