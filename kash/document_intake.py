"""Document-upload contribution intake: a file becomes a reviewed proposal, never a row.

The flow the owner approved: a contributor uploads a listing sheet (pdf/docx/doc) or a
screenshot → the bytes are quarantined in their workspace → bounded local text extraction
→ LLM field extraction (kash.intake_extraction — schema-forced, allow-listed, fail-closed
to the literal ``field: value`` parser) → a workspace document draft with a preview reply
→ the contributor fixes/confirms with the existing draft verbs plus the new
``/mine submit <draft id>`` → a pending ``listing_proposals`` row (add / correct / retire,
auto-classified against the shared pool) with the document re-linked as F3 evidence → the
owner is pinged, reviews on F3, and the submitter is pinged on the decision.

Write surface: workspace tables, ``listing_proposals``, ``contribution_attachments``,
``notification_delivery``. Never ``listings`` — publish stays owner-driven.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .contributor_document_drafts import _parse_candidate_lines, extract_document_text
from .dedup import match_key as compute_match_key
from .intake_extraction import extract_fields

_RETIRE_STATUSES = ("sold", "off_market")
_IDENTITY_HINT = "street_address + zip (or mls_number)"


def _field_lines(candidates: dict) -> list[str]:
    return [f"{name}: {candidates[name]}" for name in sorted(candidates)]


def _diff_lines(current: dict, fields: dict) -> list[str]:
    return [f"{name}: {current.get(name)} -> {fields[name]}" for name in sorted(fields)]


class DocumentIntakeService:
    """Composes the existing workspace, proposal, attachment, and notifier services."""

    def __init__(self, workspace_service, contributions, attachments, store, *,
                 notifier=None):
        self.workspace_service = workspace_service
        self.contributions = contributions
        self.attachments = attachments      # pool-side AttachmentService (F3 evidence)
        self.store = store                  # shared pool, read here only for classification
        self.notifier = notifier

    # --- upload → draft + preview --------------------------------------------------------

    def intake_document(self, actor_id, filename, content, *, caption="",
                        backend=None) -> str:
        """Stage the bytes, extract what we can, create a draft, and return the preview."""
        with self.workspace_service._workspace(actor_id) as (workspace, record):
            attachments = self.workspace_service._attachments(workspace, record)
            staged = attachments.stage_mine(actor_id, None, filename, content)
            text = extract_document_text(staged["path"], staged["media_kind"])
            candidates = self._candidates(text, caption, backend)
            draft = workspace.create_document_draft(actor_id, staged["id"], candidates)
        if not candidates_meaningful(candidates):
            if staged["media_kind"] in ("jpg", "jpeg", "png"):
                return (
                    f"Screenshot saved as evidence (attachment {staged['id']}, draft "
                    f"{draft['id']}). I can't read text out of an image — type the "
                    f"details with /mine draft edit {draft['id']} (street_address, zip, "
                    f"list_price, status, source_url, observed_at), then "
                    f"/mine submit {draft['id']}."
                )
            return (
                f"Document received and quarantined (attachment {staged['id']}, draft "
                f"{draft['id']}), but I couldn't read usable listing details out of it "
                f"(it may be scanned). Type them with /mine draft edit {draft['id']} "
                f"(field: value lines), then /mine submit {draft['id']} — or resend a "
                "clearer file."
            )
        head = (f"Document received and quarantined (attachment {staged['id']}, "
                f"draft {draft['id']}).")
        return "\n".join([head, self._preview(draft["id"], candidates)])

    def _candidates(self, text, caption, backend) -> dict:
        """LLM extraction over untrusted text, literal parsing as floor and fallback."""
        literal = _parse_candidate_lines(text) if text else {}
        out = literal
        if backend is not None and (text or caption):
            try:
                extracted = extract_fields(backend, text or "", caption)
                # literal `field: value` lines are explicit statements — they fill gaps
                # the model left, and the model's reading wins where both exist.
                out = {**literal, **extracted}
            except Exception:  # noqa: BLE001 — ladder down: degrade, never fail intake
                out = literal
        if out and "observed_at" not in out:
            out["observed_at"] = date.today().isoformat()
        return out

    # --- classification + preview --------------------------------------------------------

    def _classify(self, fields: dict):
        """(kind, key, current_row_or_None) against the live pool."""
        key = compute_match_key(fields)
        current = self.store.get(key) if key else None
        if current is None:
            return "add", key, None
        if str(fields.get("status") or "").lower() in _RETIRE_STATUSES:
            return "retire", key, current
        return "correct", key, current

    def _missing(self, candidates: dict) -> list[str]:
        missing = []
        if not compute_match_key(candidates):
            missing.append(_IDENTITY_HINT)
        if not str(candidates.get("source_url") or "").strip():
            missing.append("source_url (https link the document came from)")
        return missing

    def _preview(self, draft_id, candidates: dict) -> str:
        fields = {k: v for k, v in candidates.items()
                  if k not in ("source_url", "observed_at")}
        kind, key, current = self._classify(fields)
        lines: list[str]
        if kind == "add":
            lines = [f"Here's what I read — draft {draft_id} would submit an ADD:"]
            lines += _field_lines(candidates)
        elif kind == "retire":
            status = str(fields.get("status")).upper()
            lines = [f"The document reports this listing is {status}; draft {draft_id} "
                     f"would submit a RETIRE for {key}."]
        else:
            changed = {f: v for f, v in fields.items() if current.get(f) != v}
            if changed:
                lines = [f"The pool already tracks {key} — draft {draft_id} would submit "
                         "a CORRECTION (changed fields only):"]
                lines += _diff_lines(current, changed)
            else:
                lines = [f"The pool already tracks {key} and this document matches it — "
                         "nothing would change."]
        missing = self._missing(candidates)
        if missing:
            lines.append("Still needed before submitting: " + "; ".join(missing) + ".")
        lines += [
            "Nothing has been submitted yet.",
            f"- /mine submit {draft_id} — send it to the owner for review",
            f"- /mine draft edit {draft_id} (then field: value lines) — fix anything",
            f"- /mine discard {draft_id} — drop it",
        ]
        return "\n".join(lines)

    # --- submit → pending proposal + evidence + ping -------------------------------------

    def submit_draft(self, actor_id, draft_id) -> str:
        with self.workspace_service._workspace(actor_id) as (workspace, record):
            attachments = self.workspace_service._attachments(workspace, record)
            draft = workspace.get_document_draft(actor_id, draft_id)
            if draft["status"] == "confirmed":
                outcome = draft.get("outcome") or {}
                pid = outcome.get("proposal_id")
                if pid:
                    return (f"Draft {draft['id']} was already submitted as proposal "
                            f"#{pid} — it is with the owner for review.")
                return f"Draft {draft['id']} is already closed."
            if draft["status"] not in ("pending", "confirming"):
                raise ValueError("document draft is not pending")

            candidates = dict(draft["candidates"])
            missing = self._missing(candidates)
            if missing:
                raise ValueError(
                    "still needed: " + "; ".join(missing)
                    + f". Add them with /mine draft edit {draft['id']}")
            source_url = str(candidates.pop("source_url"))
            observed_at = str(candidates.pop("observed_at", "")) or None
            if observed_at is None:
                raise ValueError(
                    f"observed_at is required. Add it with /mine draft edit {draft['id']}")
            fields = candidates
            kind, key, current = self._classify(fields)
            changed = {}
            if kind == "correct":
                changed = {f: v for f, v in fields.items() if current.get(f) != v}
                if not changed:
                    raise ValueError(
                        "this matches what the pool already has — nothing to submit")

            # Pin the identity before any cross-store write so a dropped Telegram reply
            # is safe to retry (the existing draft state machine).
            if draft["status"] == "pending":
                draft = workspace.begin_document_draft_confirmation(
                    actor_id, draft["id"], key)
            elif draft["match_key"] != key:
                raise ValueError("document draft identity changed unexpectedly")

            # Evidence bytes FIRST: if the 30-day purge already removed them, stop before
            # creating anything.
            evidence = attachments.file_for_mine(actor_id, draft["attachment_id"])
            content = Path(evidence["path"]).read_bytes()

            proposal = self._existing_pending(actor_id, kind, key, fields)
            if proposal is None:
                if kind == "add":
                    proposal = self.contributions.submit_add(
                        actor_id, fields, source_url=source_url, observed_at=observed_at)
                elif kind == "correct":
                    proposal = self.contributions.submit_correction(
                        actor_id, key, changed, source_url=source_url,
                        observed_at=observed_at)
                else:
                    reason = f"document reports status {fields.get('status')}"
                    if fields.get("sold_date"):
                        reason += f" (sold_date {fields['sold_date']})"
                    proposal = self.contributions.submit_retire(
                        actor_id, key, reason=reason, source_url=source_url,
                        observed_at=observed_at)

            evidence_note = self._link_evidence(actor_id, proposal["id"], evidence, content)
            if self.notifier is not None:
                self.notifier.submitted(proposal["id"])
            workspace.complete_document_draft_confirmation(
                actor_id, draft["id"], {"proposal_id": proposal["id"], "kind": kind})

        summary = fields.get("street_address") or key or "listing"
        reply = (f"Proposal #{proposal['id']} ({kind} — {summary}) submitted and pending "
                 "owner review. It has not changed any listing. You'll get a message "
                 "here when it's reviewed.")
        if evidence_note:
            reply += f"\n{evidence_note}"
        return reply

    def _existing_pending(self, actor_id, kind, key, fields):
        """Retry safety: a crash after proposal creation must not create a twin. An add
        matches on address identity, correct/retire on the target match_key."""
        rows = self.store.conn.execute(
            "SELECT id, payload_json, source_url, observed_at FROM listing_proposals"
            " WHERE submitted_by=? AND kind=? AND status='pending'",
            (int(actor_id), kind)).fetchall()
        for row in rows:
            try:
                payload = json.loads(row[1])
            except (TypeError, ValueError):
                continue
            existing_key = (compute_match_key(payload) if kind == "add"
                            else payload.get("match_key"))
            if existing_key and existing_key == key:
                return {"id": int(row[0]), "kind": kind, "payload": payload,
                        "source_url": row[2], "observed_at": row[3]}
        return None

    def _link_evidence(self, actor_id, proposal_id, evidence, content):
        """Copy the already-quarantined bytes into the proposal quarantine for F3. Never
        fatal — the proposal stands even if the evidence link fails."""
        try:
            already = self.store.conn.execute(
                "SELECT 1 FROM contribution_attachments WHERE proposal_id=? AND sha256=?",
                (int(proposal_id), evidence["sha256"])).fetchone()
            if already:
                return None
            self.attachments.stage(actor_id, proposal_id, evidence["stored_name"], content)
            return None
        except Exception:  # noqa: BLE001
            return (f"(Your document could not be linked as evidence — attach it with a "
                    f"caption of /attach {int(proposal_id)}.)")


def candidates_meaningful(candidates: dict) -> bool:
    """More than the observed_at default the sanitizer always supplies."""
    return bool(set(candidates) - {"observed_at"})


class IntakeCommands:
    """Deterministic dispatch for the one new verb; everything else falls through to
    MineCommands. Never interprets with an LLM."""

    def __init__(self, service):
        self.service = service

    def try_handle(self, actor_id, text):
        raw = (text or "").strip()
        first = raw.splitlines()[0].split() if raw else []
        if not first or first[0].lower().split("@", 1)[0] != "/mine":
            return None
        if len(first) == 3 and first[1].lower() == "submit":
            try:
                draft_id = int(first[2])
            except ValueError:
                return "Use /mine submit <draft id> — see /mine drafts for your drafts."
            try:
                return self.service.submit_draft(actor_id, draft_id)
            except PermissionError:
                return "An enabled contributor workspace is required."
            except ValueError as exc:
                return f"Submission was not accepted: {exc}."
        if len(first) == 2 and first[1].lower() == "submit":
            return "Use /mine submit <draft id> — see /mine drafts for your drafts."
        return None
