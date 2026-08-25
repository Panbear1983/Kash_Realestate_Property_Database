"""Isolated, auditable listing store owned by one contributor workspace."""
from __future__ import annotations

import ipaddress
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .contributions import PUBLIC_EDITABLE_FIELDS
from .dedup import match_key
from .schema import Listing


class WorkspaceStore:
    """A contributor-scoped store; callers never receive SQL or another workspace's data."""

    def __init__(self, path, *, contributor_id):
        self.path = Path(path)
        self.contributor_id = int(contributor_id)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS workspace_listings (
                match_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                revision INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                retired_at TEXT,
                deleted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS workspace_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_key TEXT NOT NULL,
                revision INTEGER NOT NULL,
                actor_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                reason TEXT,
                ts TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspace_document_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attachment_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                candidate_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                match_key TEXT,
                outcome_json TEXT
            );
            CREATE TABLE IF NOT EXISTS workspace_document_draft_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id INTEGER NOT NULL,
                actor_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                ts TEXT NOT NULL
            );
            """
        )
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(workspace_audit)")}
        if "reason" not in columns:
            self.conn.execute("ALTER TABLE workspace_audit ADD COLUMN reason TEXT")
        draft_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(workspace_document_drafts)")}
        if "match_key" not in draft_columns:
            self.conn.execute("ALTER TABLE workspace_document_drafts ADD COLUMN match_key TEXT")
        if "outcome_json" not in draft_columns:
            self.conn.execute("ALTER TABLE workspace_document_drafts ADD COLUMN outcome_json TEXT")
        self.conn.commit()

    def close(self):
        self.conn.close()

    def add(self, actor_id, fields):
        self._require_owner(actor_id)
        payload = self._validate(fields)
        key = match_key(payload)
        if not key:
            raise ValueError("listing requires a stable address or MLS identity")
        if self._row(key):
            raise ValueError("workspace listing already exists")
        now = self._now()
        self.conn.execute(
            """INSERT INTO workspace_listings
               (match_key,payload_json,status,revision,created_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (key, self._dump(payload), payload.get("status", "active"), 1, now, now),
        )
        self._audit(key, 1, actor_id, "add", None, payload)
        self.conn.commit()
        return self._record(self._row(key))

    def get(self, actor_id, key):
        self._require_owner(actor_id)
        row = self._row(key)
        if not row:
            raise ValueError("workspace listing does not exist")
        return self._record(row)

    def find(self, actor_id, key):
        """Look up a listing by its deterministic match key without raising if absent."""
        self._require_owner(actor_id)
        row = self._row(key)
        return self._record(row) if row else None

    def list(self, actor_id, *, include_deleted=False):
        self._require_owner(actor_id)
        query = "SELECT * FROM workspace_listings"
        if not include_deleted:
            query += " WHERE deleted_at IS NULL"
        return [self._record(row) for row in self.conn.execute(query + " ORDER BY updated_at DESC, rowid DESC")]

    def update(self, actor_id, key, fields):
        self._require_owner(actor_id)
        row = self._required_row(key)
        if row[7] is not None:
            raise ValueError("deleted workspace listing cannot be edited")
        update = self._validate(fields, allow_partial=True)
        before = self._payload(row)
        after = {**before, **update}
        revision = int(row[3]) + 1
        now = self._now()
        self.conn.execute(
            "UPDATE workspace_listings SET payload_json=?,status=?,revision=?,updated_at=? WHERE match_key=?",
            (self._dump(after), after.get("status", row[2]), revision, now, key),
        )
        self._audit(key, revision, actor_id, "update", before, after)
        self.conn.commit()
        return self._record(self._row(key))

    def retire(self, actor_id, key, *, reason):
        self._require_owner(actor_id)
        reason = self._reason(reason)
        row = self._required_row(key)
        before = self._payload(row)
        after = {**before, "status": "off_market"}
        revision = int(row[3]) + 1
        now = self._now()
        self.conn.execute(
            """UPDATE workspace_listings SET payload_json=?,status='off_market',revision=?,
               updated_at=?,retired_at=? WHERE match_key=?""",
            (self._dump(after), revision, now, now, key),
        )
        self._audit(key, revision, actor_id, "retire", before, after, reason=reason)
        self.conn.commit()
        return self._record(self._row(key))

    def restore(self, actor_id, key, *, reason):
        self._require_owner(actor_id)
        reason = self._reason(reason)
        row = self._required_row(key)
        before = self._payload(row)
        after = {**before, "status": "active"}
        revision = int(row[3]) + 1
        now = self._now()
        self.conn.execute(
            """UPDATE workspace_listings SET payload_json=?,status='active',revision=?,
               updated_at=?,retired_at=NULL,deleted_at=NULL WHERE match_key=?""",
            (self._dump(after), revision, now, key),
        )
        self._audit(key, revision, actor_id, "restore", before, after, reason=reason)
        self.conn.commit()
        return self._record(self._row(key))

    def delete(self, actor_id, key, *, reason):
        self._require_owner(actor_id)
        reason = self._reason(reason)
        row = self._required_row(key)
        if row[7] is not None:
            raise ValueError("workspace listing is already deleted")
        before = self._payload(row)
        revision = int(row[3]) + 1
        now = self._now()
        self.conn.execute(
            "UPDATE workspace_listings SET revision=?,updated_at=?,deleted_at=? WHERE match_key=?",
            (revision, now, now, key),
        )
        self._audit(key, revision, actor_id, "delete", before, before, reason=reason)
        self.conn.commit()
        return self._record(self._row(key))

    def audit(self, actor_id, key):
        self._require_owner(actor_id)
        return [dict(row) for row in self.conn.execute(
            "SELECT match_key,revision,actor_id,event,before_json,after_json,reason,ts FROM workspace_audit WHERE match_key=? ORDER BY id",
            (key,),
        )]

    def create_document_draft(self, actor_id, attachment_id, candidates):
        """Persist an owner-only, non-listing candidate set linked to a staged document."""
        self._require_owner(actor_id)
        values = self._draft_candidates(candidates)
        attachment = self.conn.execute(
            """SELECT id FROM workspace_attachments
               WHERE id=? AND uploaded_by=? AND media_kind IN ('doc','docx','pdf')
                 AND status IN ('quarantined','manual_review')""",
            (int(attachment_id), int(actor_id)),
        ).fetchone()
        if not attachment:
            raise ValueError("document attachment is unavailable")
        now = self._now()
        cursor = self.conn.execute(
            """INSERT INTO workspace_document_drafts
               (attachment_id,owner_id,candidate_json,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (int(attachment_id), int(actor_id), self._dump(values), "pending", now, now),
        )
        draft = self._draft_record(self._draft_row(cursor.lastrowid))
        self._draft_audit(draft["id"], actor_id, "create", None, draft)
        self.conn.commit()
        return draft

    def get_document_draft(self, actor_id, draft_id):
        self._require_owner(actor_id)
        row = self._draft_row(draft_id)
        if not row or int(row[2]) != int(actor_id):
            raise ValueError("document draft does not exist")
        return self._draft_record(row)

    def list_document_drafts(self, actor_id):
        self._require_owner(actor_id)
        return [self._draft_record(row) for row in self.conn.execute(
            """SELECT * FROM workspace_document_drafts
               WHERE owner_id=? AND status='pending' ORDER BY updated_at DESC, id DESC""",
            (int(actor_id),),
        )]

    def update_document_draft(self, actor_id, draft_id, fields):
        """Allow the owning contributor to correct bounded typed candidates before confirmation."""
        draft = self.get_document_draft(actor_id, draft_id)
        if draft["status"] != "pending":
            raise ValueError("document draft is not pending")
        changes = self._draft_candidates(fields)
        updated_values = {**draft["candidates"], **changes}
        now = self._now()
        self.conn.execute(
            "UPDATE workspace_document_drafts SET candidate_json=?,updated_at=? WHERE id=?",
            (self._dump(updated_values), now, draft["id"]),
        )
        updated = self._draft_record(self._draft_row(draft["id"]))
        self._draft_audit(draft["id"], actor_id, "update", draft, updated)
        self.conn.commit()
        return updated

    def discard_document_draft(self, actor_id, draft_id):
        draft = self.get_document_draft(actor_id, draft_id)
        if draft["status"] != "pending":
            raise ValueError("document draft is not pending")
        now = self._now()
        self.conn.execute("UPDATE workspace_document_drafts SET status='discarded',updated_at=? WHERE id=?", (now, draft["id"]))
        discarded = self._draft_record(self._draft_row(draft["id"]))
        self._draft_audit(draft["id"], actor_id, "discard", draft, discarded)
        self.conn.commit()
        return discarded

    def validate_public_fields(self, fields):
        """Validate candidate fields the same way `add` would, without writing anything."""
        return self._validate(fields)

    def begin_document_draft_confirmation(self, actor_id, draft_id, match_key):
        """Move a validated, pending draft to 'confirming' and durably pin its identity.

        Safe to call again for a draft already in 'confirming': it is a no-op as long as the
        recomputed match key still agrees with the one recorded on the first attempt, which is
        what lets a crashed/retried confirmation resume instead of re-deriving a new identity.
        """
        draft = self.get_document_draft(actor_id, draft_id)
        if draft["status"] == "confirming":
            if draft["match_key"] != match_key:
                raise ValueError("document draft identity changed unexpectedly")
            return draft
        if draft["status"] != "pending":
            raise ValueError("document draft is not pending")
        now = self._now()
        self.conn.execute(
            "UPDATE workspace_document_drafts SET status='confirming',match_key=?,updated_at=? WHERE id=?",
            (match_key, now, draft["id"]),
        )
        updated = self._draft_record(self._draft_row(draft["id"]))
        self._draft_audit(draft["id"], actor_id, "begin_confirm", draft, updated)
        self.conn.commit()
        return updated

    def complete_document_draft_confirmation(self, actor_id, draft_id, outcome):
        """Record the durable outcome of a confirmation and close out the draft.

        Idempotent: a draft already 'confirmed' just returns its recorded outcome again.
        """
        draft = self.get_document_draft(actor_id, draft_id)
        if draft["status"] == "confirmed":
            return draft
        if draft["status"] != "confirming":
            raise ValueError("document draft is not confirming")
        now = self._now()
        self.conn.execute(
            "UPDATE workspace_document_drafts SET status='confirmed',outcome_json=?,updated_at=? WHERE id=?",
            (self._dump(outcome), now, draft["id"]),
        )
        confirmed = self._draft_record(self._draft_row(draft["id"]))
        self._draft_audit(draft["id"], actor_id, "confirm", draft, confirmed)
        self.conn.commit()
        return confirmed

    def _required_row(self, key):
        row = self._row(key)
        if not row:
            raise ValueError("workspace listing does not exist")
        return row

    def _row(self, key):
        return self.conn.execute("SELECT * FROM workspace_listings WHERE match_key=?", (key,)).fetchone()

    def _draft_row(self, draft_id):
        return self.conn.execute("SELECT * FROM workspace_document_drafts WHERE id=?", (int(draft_id),)).fetchone()

    def _record(self, row):
        payload = self._payload(row)
        return {**payload, "match_key": row[0], "status": row[2], "revision": int(row[3]),
                "created_at": row[4], "updated_at": row[5], "retired_at": row[6], "deleted_at": row[7]}

    def _payload(self, row):
        return json.loads(row[1])

    def _draft_record(self, row):
        return {"id": int(row[0]), "attachment_id": int(row[1]), "owner_id": int(row[2]),
                "candidates": json.loads(row[3]), "status": row[4], "created_at": row[5], "updated_at": row[6],
                "match_key": row[7], "outcome": json.loads(row[8]) if row[8] else None}

    def _audit(self, key, revision, actor_id, event, before, after, *, reason=None):
        self.conn.execute(
            """INSERT INTO workspace_audit(match_key,revision,actor_id,event,before_json,after_json,reason,ts)
               VALUES(?,?,?,?,?,?,?,?)""",
            (key, revision, int(actor_id), event, self._dump(before) if before else None,
             self._dump(after) if after else None, reason, self._now()),
        )

    def _draft_audit(self, draft_id, actor_id, event, before, after):
        self.conn.execute(
            """INSERT INTO workspace_document_draft_audit(draft_id,actor_id,event,before_json,after_json,ts)
               VALUES(?,?,?,?,?,?)""",
            (int(draft_id), int(actor_id), event, self._dump(before) if before else None,
             self._dump(after) if after else None, self._now()),
        )

    def _require_owner(self, actor_id):
        if int(actor_id) != self.contributor_id:
            raise PermissionError("workspace access is limited to its contributor")

    @staticmethod
    def _validate(fields, *, allow_partial=False):
        if not isinstance(fields, dict) or not fields:
            raise ValueError("a non-empty structured field object is required")
        forbidden = sorted(set(fields) - PUBLIC_EDITABLE_FIELDS - {"source_url", "observed_at"})
        if forbidden:
            raise ValueError("fields not permitted in contributor workspace: " + ", ".join(forbidden))
        source_url = fields.get("source_url")
        if source_url is not None:
            parsed = urlparse(str(source_url))
            host = parsed.hostname
            if (parsed.scheme != "https" or not host or parsed.username or parsed.password
                    or not WorkspaceStore._is_public_host(host)):
                raise ValueError("a public HTTPS source URL is required")
        observed_at = fields.get("observed_at")
        if observed_at is not None:
            try:
                date.fromisoformat(str(observed_at))
            except ValueError as exc:
                raise ValueError("observed_at must be an ISO date") from exc
        model_fields = {key: value for key, value in fields.items() if key not in {"source_url", "observed_at"}}
        validated = Listing.model_validate(model_fields).model_dump(
            exclude_none=True, exclude_unset=allow_partial
        )
        if source_url is not None:
            validated["source_url"] = str(source_url)
        if observed_at is not None:
            validated["observed_at"] = str(observed_at)
        if not allow_partial and not match_key(validated):
            raise ValueError("listing requires a stable address or MLS identity")
        return validated

    @staticmethod
    def _draft_candidates(candidates):
        if not isinstance(candidates, dict) or len(candidates) > 32:
            raise ValueError("document draft candidates must be a bounded field object")
        values = {}
        allowed = PUBLIC_EDITABLE_FIELDS | {"source_url", "observed_at"}
        forbidden = sorted(set(candidates) - allowed)
        if forbidden:
            raise ValueError("field not permitted in document draft: " + ", ".join(forbidden))
        for key, value in candidates.items():
            if not isinstance(key, str) or not key or len(key) > 64 or not isinstance(value, (str, int, float, bool)):
                raise ValueError("document draft candidates must contain bounded scalar fields")
            text = str(value).strip()
            if not text or len(text) > 1000:
                raise ValueError("document draft candidates must contain non-empty bounded values")
            values[key] = value
        return values

    @staticmethod
    def _dump(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    @staticmethod
    def _is_public_host(host):
        lowered = str(host).strip().lower().rstrip(".")
        if not lowered or lowered == "localhost" or lowered.endswith(".localhost"):
            return False
        try:
            address = ipaddress.ip_address(lowered)
        except ValueError:
            return True
        return not (address.is_private or address.is_loopback or address.is_link_local
                    or address.is_multicast or address.is_reserved or address.is_unspecified)

    @staticmethod
    def _reason(value):
        text = str(value or "").strip()
        if not text or len(text) > 240:
            raise ValueError("reason must be 1–240 characters")
        return text
