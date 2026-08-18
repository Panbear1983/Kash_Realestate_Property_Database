"""Structured, staged listing proposals. This module never publishes a listing."""
from __future__ import annotations

import json
from datetime import date
from urllib.parse import urlparse

from .data_roles import DataRoles, ROLE_OWNER
from .dedup import match_key
from .schema import CALC_FIELDS, FIELD_ORDER, USER_PROTECTED, Listing

PROPOSAL_ADD = "add"
PROPOSAL_CORRECT = "correct"
PROPOSAL_RETIRE = "retire"
PROPOSAL_KINDS = frozenset({PROPOSAL_ADD, PROPOSAL_CORRECT, PROPOSAL_RETIRE})
PUBLIC_EDITABLE_FIELDS = frozenset(FIELD_ORDER) - USER_PROTECTED - CALC_FIELDS - {
    "rank", "tier", "target_buy_price", "arv_estimate", "brrrr_rating", "bid_estimate",
    "investment_thesis", "analysis", "my_notes", "favorite", "viewing_status",
    "viewing_date", "offer_status", "user_rating", "contacted_agent",
    # Provenance is the system's, never a contributor's: a payload carrying `source`
    # would erase the human-reviewed:<id> marker publish() just stamped, and forged
    # first_seen/fetched dates would fire new-listing alerts.
    "source", "source_url", "first_seen_date", "fetched_at", "last_updated",
    "property_id",
}


class ContributionService:
    def __init__(self, store, roles: DataRoles | None = None):
        self.store = store
        self.roles = roles or DataRoles(store)
        self.conn = store.conn
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS listing_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submitted_by INTEGER NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                source_url TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS proposal_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id INTEGER NOT NULL,
                reviewer_id INTEGER NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS publish_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id INTEGER UNIQUE NOT NULL,
                published_by INTEGER NOT NULL,
                outcome TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS rollback_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id INTEGER NOT NULL,
                rolled_back_by INTEGER NOT NULL,
                reason TEXT NOT NULL,
                ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        self.conn.commit()

    def submit_add(self, actor_id, fields, *, source_url, observed_at):
        self._require_submitter(actor_id)
        payload = self._validate_fields(fields)
        self._validate_source(source_url, observed_at)
        # An "add" for an address the pool already tracks would MERGE on publish (upsert
        # by match_key) with publish_audit.before_json = null — an unrecoverable
        # overwrite disguised as an insert. Force the correction path instead.
        existing_key = match_key(payload)
        if existing_key and self.store.get(existing_key) is not None:
            raise ValueError(
                f"a listing for this address already exists ({existing_key}); "
                "submit a correction instead of an add")
        cursor = self.conn.execute(
            "INSERT INTO listing_proposals(submitted_by,kind,status,payload_json,source_url,observed_at) VALUES(?,?,?,?,?,?)",
            (int(actor_id), PROPOSAL_ADD, "pending", json.dumps(payload, sort_keys=True), source_url, observed_at),
        )
        self.conn.commit()
        return {"id": cursor.lastrowid, "submitted_by": int(actor_id), "kind": PROPOSAL_ADD,
                "status": "pending", "payload": payload, "source_url": source_url,
                "observed_at": observed_at}

    def submit_correction(self, actor_id, match_key, fields, *, source_url, observed_at):
        self._require_submitter(actor_id)
        if not str(match_key).strip():
            raise ValueError("target match_key is required")
        fields = self._validate_fields(fields)
        current = self.store.get(str(match_key))
        if current is None:
            raise ValueError("correction target does not exist")
        payload = {"match_key": str(match_key), "fields": fields,
                   "expected": {field: current.get(field) for field in fields}}
        return self._stage(actor_id, PROPOSAL_CORRECT, payload, source_url, observed_at)

    def submit_retire(self, actor_id, match_key, *, reason, source_url, observed_at):
        self._require_submitter(actor_id)
        if not str(match_key).strip():
            raise ValueError("target match_key is required")
        payload = {"match_key": str(match_key), "reason": self._review_reason(reason)}
        return self._stage(actor_id, PROPOSAL_RETIRE, payload, source_url, observed_at)

    def _stage(self, actor_id, kind, payload, source_url, observed_at):
        self._validate_source(source_url, observed_at)
        cursor = self.conn.execute(
            "INSERT INTO listing_proposals(submitted_by,kind,status,payload_json,source_url,observed_at) VALUES(?,?,?,?,?,?)",
            (int(actor_id), kind, "pending", json.dumps(payload, sort_keys=True), source_url, observed_at),
        )
        self.conn.commit()
        return {"id": cursor.lastrowid, "submitted_by": int(actor_id), "kind": kind, "status": "pending",
                "payload": payload, "source_url": source_url, "observed_at": observed_at}

    def approve(self, actor_id, proposal_id, *, reason):
        if not self.roles.can_review(actor_id):
            raise PermissionError("reviewer role required")
        row = self.conn.execute(
            "SELECT submitted_by,status,kind FROM listing_proposals WHERE id=?", (int(proposal_id),)
        ).fetchone()
        if not row:
            raise ValueError("proposal not found")
        if int(row[0]) == int(actor_id):
            raise PermissionError("contributors cannot approve their own proposals")
        if row[1] != "pending":
            raise ValueError("only pending proposals may be approved")
        text = self._review_reason(reason)
        self.conn.execute("UPDATE listing_proposals SET status='approved' WHERE id=?", (int(proposal_id),))
        self.conn.execute(
            "INSERT INTO proposal_reviews(proposal_id,reviewer_id,decision,reason) VALUES(?,?,?,?)",
            (int(proposal_id), int(actor_id), "approved", text),
        )
        self.conn.commit()
        return {"id": int(proposal_id), "status": "approved", "reviewed_by": int(actor_id)}

    def reject(self, actor_id, proposal_id, *, reason):
        if not self.roles.can_review(actor_id):
            raise PermissionError("reviewer role required")
        row = self.conn.execute(
            "SELECT submitted_by,status FROM listing_proposals WHERE id=?", (int(proposal_id),)
        ).fetchone()
        if not row:
            raise ValueError("proposal not found")
        if int(row[0]) == int(actor_id):
            raise PermissionError("contributors cannot review their own proposals")
        if row[1] != "pending":
            raise ValueError("only pending proposals may be rejected")
        text = self._review_reason(reason)
        self.conn.execute("UPDATE listing_proposals SET status='rejected' WHERE id=?", (int(proposal_id),))
        self.conn.execute(
            "INSERT INTO proposal_reviews(proposal_id,reviewer_id,decision,reason) VALUES(?,?,?,?)",
            (int(proposal_id), int(actor_id), "rejected", text),
        )
        self.conn.commit()
        return {"id": int(proposal_id), "status": "rejected", "reviewed_by": int(actor_id)}

    def withdraw(self, actor_id, proposal_id, *, reason):
        row = self.conn.execute(
            "SELECT submitted_by,status FROM listing_proposals WHERE id=?", (int(proposal_id),)
        ).fetchone()
        if not row:
            raise ValueError("proposal not found")
        if int(row[0]) != int(actor_id):
            raise PermissionError("contributors may withdraw only their own proposals")
        if row[1] != "pending":
            raise ValueError("only pending proposals may be withdrawn")
        text = self._review_reason(reason)
        self.conn.execute("UPDATE listing_proposals SET status='withdrawn' WHERE id=?", (int(proposal_id),))
        self.conn.execute(
            "INSERT INTO proposal_reviews(proposal_id,reviewer_id,decision,reason) VALUES(?,?,?,?)",
            (int(proposal_id), int(actor_id), "withdrawn", text),
        )
        self.conn.commit()
        return {"id": int(proposal_id), "status": "withdrawn", "reviewed_by": int(actor_id)}

    def publish(self, actor_id, proposal_id):
        if not self.roles.can_publish(actor_id):
            raise PermissionError("publisher role required")
        row = self.conn.execute(
            "SELECT kind,status,payload_json,source_url FROM listing_proposals WHERE id=?", (int(proposal_id),)
        ).fetchone()
        if not row:
            raise ValueError("proposal not found")
        if row[1] != "approved":
            raise ValueError("only approved proposals may be published")
        payload = json.loads(row[2])
        if row[0] == PROPOSAL_ADD:
            before = None
            outcome = self.store.upsert(payload, source=f"human-reviewed:{int(proposal_id)}")
            if outcome == "rejected":
                raise ValueError("approved proposal did not produce a valid listing identity")
            from .dedup import match_key
            after = self.store.get(match_key(payload))
        elif row[0] == PROPOSAL_CORRECT:
            key = payload.get("match_key")
            before = self.store.get(key)
            if before is None:
                raise ValueError("correction target no longer exists")
            expected = payload.get("expected") or {}
            if any(before.get(field) != value for field, value in expected.items()):
                raise ValueError("correction target changed since submission")
            if not self.store.update_fields(key, payload.get("fields") or {}):
                raise ValueError("correction contains no applicable fields")
            after = self.store.get(key)
            outcome = "corrected"
        elif row[0] == PROPOSAL_RETIRE:
            key = payload.get("match_key")
            before = self.store.get(key)
            if before is None:
                raise ValueError("retire target no longer exists")
            if not self.store.update_fields(key, {"status": "off_market"}):
                raise ValueError("retire target could not be archived")
            after = self.store.get(key)
            outcome = "retired"
        else:
            raise ValueError("unsupported approved proposal type")
        self.conn.execute("UPDATE listing_proposals SET status='published' WHERE id=?", (int(proposal_id),))
        self.conn.execute(
            "INSERT INTO publish_audit(proposal_id,published_by,outcome,before_json,after_json) VALUES(?,?,?,?,?)",
            (int(proposal_id), int(actor_id), outcome, json.dumps(before), json.dumps(after, sort_keys=True)),
        )
        self.conn.commit()
        audit = {"proposal_id": int(proposal_id), "published_by": int(actor_id), "outcome": outcome,
                 "before": before, "after": after}
        return {"id": int(proposal_id), "status": "published", "audit": audit}

    def rollback(self, actor_id, proposal_id, *, reason):
        if self.roles.role_for(actor_id) != ROLE_OWNER:
            raise PermissionError("owner role required for rollback")
        row = self.conn.execute(
            """SELECT p.kind, a.before_json FROM listing_proposals p
               JOIN publish_audit a ON a.proposal_id=p.id WHERE p.id=? AND p.status='published'""",
            (int(proposal_id),),
        ).fetchone()
        if not row:
            raise ValueError("published proposal audit not found")
        if row[0] != PROPOSAL_RETIRE:
            raise ValueError("only soft-retire rollback is supported in this slice")
        before = json.loads(row[1] or "null")
        key = (json.loads(self.conn.execute("SELECT payload_json FROM listing_proposals WHERE id=?", (int(proposal_id),)).fetchone()[0])
               .get("match_key"))
        prior_status = (before or {}).get("status")
        if not key or not prior_status or not self.store.update_fields(key, {"status": prior_status}):
            raise ValueError("could not reinstate prior listing status")
        text = self._review_reason(reason)
        self.conn.execute(
            "INSERT INTO rollback_audit(proposal_id,rolled_back_by,reason) VALUES(?,?,?)",
            (int(proposal_id), int(actor_id), text),
        )
        self.conn.commit()
        return {"proposal_id": int(proposal_id), "outcome": "reinstated", "status": prior_status}

    def _require_submitter(self, actor_id):
        if not self.roles.can_submit(actor_id):
            raise PermissionError("data contributor role required")

    @staticmethod
    def _review_reason(value):
        text = str(value or "").strip()
        if not text or len(text) > 240:
            raise ValueError("review reason must be 1–240 characters")
        return text

    @staticmethod
    def _validate_source(source_url, observed_at):
        parsed = urlparse(str(source_url))
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("an HTTPS source URL is required")
        try:
            date.fromisoformat(str(observed_at))
        except ValueError as exc:
            raise ValueError("observed_at must be an ISO date") from exc

    @staticmethod
    def _validate_fields(fields):
        if not isinstance(fields, dict) or not fields:
            raise ValueError("a non-empty structured field object is required")
        forbidden = sorted(set(fields) - PUBLIC_EDITABLE_FIELDS)
        if forbidden:
            raise ValueError("fields not permitted in contributor proposals: " + ", ".join(forbidden))
        # Pydantic drops unknown fields, but we rejected unknown keys above; this validates types/enums.
        return Listing.model_validate(fields).model_dump(exclude_none=True)
