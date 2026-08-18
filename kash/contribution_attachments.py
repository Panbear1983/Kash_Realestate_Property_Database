"""Quarantined evidence attachments for staged contributor proposals.

This module deliberately performs no document extraction, execution, or listing writes.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from .data_roles import DataRoles

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
ATTACHMENT_RETENTION_DAYS = 30
_MAGIC = {
    ".pdf": b"%PDF-",
    ".jpg": b"\xff\xd8\xff",
    ".jpeg": b"\xff\xd8\xff",
    ".png": b"\x89PNG\r\n\x1a\n",
    ".docx": b"PK\x03\x04",
    ".doc": bytes.fromhex("D0CF11E0A1B11AE1"),
}
_MANUAL_ONLY = frozenset({".doc"})


class AttachmentService:
    """Store allow-listed evidence under opaque names outside the listing database."""

    def __init__(self, store, roles: DataRoles | None = None, *, quarantine_dir):
        self.store = store
        self.roles = roles or DataRoles(store)
        self.conn = store.conn
        self.quarantine_dir = Path(quarantine_dir).expanduser()
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS contribution_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id INTEGER NOT NULL,
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

    def purge_expired(self) -> int:
        """Delete retained file bytes after 30 days while preserving an expired audit row."""
        rows = self.conn.execute(
            """SELECT id, stored_name FROM contribution_attachments
               WHERE status IN ('quarantined','manual_review')
                 AND created_at < datetime('now', ?)""",
            (f"-{ATTACHMENT_RETENTION_DAYS} days",),
        ).fetchall()
        for attachment_id, stored_name in rows:
            (self.quarantine_dir / stored_name).unlink(missing_ok=True)
            self.conn.execute("UPDATE contribution_attachments SET status='expired' WHERE id=?", (attachment_id,))
        self.conn.commit()
        return len(rows)

    def stage(self, actor_id, proposal_id, filename, content: bytes):
        """Quarantine one bounded evidence file linked to the actor's pending proposal."""
        if not self.roles.can_submit(actor_id):
            raise PermissionError("data contributor role required")
        row = self.conn.execute(
            "SELECT submitted_by,status FROM listing_proposals WHERE id=?", (int(proposal_id),)
        ).fetchone()
        if not row or int(row[0]) != int(actor_id):
            raise PermissionError("attachments may be linked only to the contributor's own proposal")
        if row[1] != "pending":
            raise ValueError("attachments may be added only while a proposal is pending")
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
                """INSERT INTO contribution_attachments
                   (proposal_id,uploaded_by,stored_name,sha256,media_kind,byte_size,status,extraction)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (int(proposal_id), int(actor_id), stored_name, digest, suffix[1:], len(content), status, "disabled"),
            )
            self.conn.commit()
        except Exception:
            temp_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise
        return {
            "proposal_id": int(proposal_id), "sha256": digest, "media_kind": suffix[1:],
            "byte_size": len(content), "status": status, "extraction": "disabled", "path": str(final_path),
        }
