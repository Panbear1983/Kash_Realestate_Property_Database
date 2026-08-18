"""Actor-scoped contributor workspace access and deterministic Telegram /mine commands."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from .contributor_document_drafts import extract_document_candidates
from .contributor_workspace_attachments import WorkspaceAttachmentService
from .contributor_workspace_store import WorkspaceStore
from .dedup import match_key as compute_match_key


_HELP = (
    "Your workspace commands:\n"
    "/mine list\n"
    "/mine show <listing key>\n"
    "/mine add\nsource_url: https://...\nobserved_at: YYYY-MM-DD\n"
    "street_address: ...\nzip: ...\nlist_price: ...\nstatus: active\n"
    "/mine edit <listing key>\nfield: value\n"
    "/mine retire <listing key>\nreason: ...\n"
    "/mine restore <listing key>\nreason: ...\n"
    "/mine delete <listing key>\nreason: ...\n"
    "/mine draft <attachment id>\n"
    "/mine drafts\n"
    "/mine confirm <draft id>\n"
    "/mine discard <draft id>"
)


class ContributorWorkspaceService:
    """Open only an already-enabled workspace belonging to the requesting contributor."""

    def __init__(self, workspaces, autosync):
        self.workspaces = workspaces
        self.autosync = autosync

    def list(self, actor_id):
        with self._workspace(actor_id) as (workspace, _record):
            return workspace.list(actor_id)

    def show(self, actor_id, match_key):
        with self._workspace(actor_id) as (workspace, _record):
            return workspace.get(actor_id, match_key)

    def add(self, actor_id, fields):
        values = dict(fields or {})
        self._require_addition_fields(values)
        with self._workspace(actor_id) as (workspace, record):
            added = workspace.add(actor_id, values)
            return {"record": added, "sync": self._sync_result(workspace, record, added)}

    def edit(self, actor_id, match_key, fields):
        with self._workspace(actor_id) as (workspace, record):
            changed = workspace.update(actor_id, match_key, fields)
            self.autosync.hold_workspace_change(changed, workspace_key=record["workspace_key"], kind="update")
            return changed

    def retire(self, actor_id, match_key, *, reason):
        with self._workspace(actor_id) as (workspace, record):
            changed = workspace.retire(actor_id, match_key, reason=reason)
            self.autosync.hold_workspace_change(changed, workspace_key=record["workspace_key"], kind="retire")
            return changed

    def restore(self, actor_id, match_key, *, reason):
        with self._workspace(actor_id) as (workspace, record):
            changed = workspace.restore(actor_id, match_key, reason=reason)
            self.autosync.hold_workspace_change(changed, workspace_key=record["workspace_key"], kind="restore")
            return changed

    def delete(self, actor_id, match_key, *, reason):
        with self._workspace(actor_id) as (workspace, record):
            changed = workspace.delete(actor_id, match_key, reason=reason)
            self.autosync.hold_workspace_change(changed, workspace_key=record["workspace_key"], kind="delete")
            return changed

    def attach(self, actor_id, match_key, filename, content):
        with self._workspace(actor_id) as (workspace, record):
            return self._attachments(workspace, record).stage_mine(
                actor_id, match_key, filename, content
            )

    def create_draft_from_attachment(self, actor_id, attachment_id):
        with self._workspace(actor_id) as (workspace, record):
            attachment = self._attachments(workspace, record).document_for_mine(actor_id, attachment_id)
            candidates = extract_document_candidates(attachment["path"], attachment["media_kind"])
            return workspace.create_document_draft(actor_id, attachment["id"], candidates)

    def list_drafts(self, actor_id):
        with self._workspace(actor_id) as (workspace, _record):
            return workspace.list_document_drafts(actor_id)

    def show_draft(self, actor_id, draft_id):
        with self._workspace(actor_id) as (workspace, _record):
            return workspace.get_document_draft(actor_id, draft_id)

    def edit_draft(self, actor_id, draft_id, fields):
        with self._workspace(actor_id) as (workspace, _record):
            return workspace.update_document_draft(actor_id, draft_id, fields)

    def discard_draft(self, actor_id, draft_id):
        with self._workspace(actor_id) as (workspace, _record):
            return workspace.discard_document_draft(actor_id, draft_id)

    def confirm_draft(self, actor_id, draft_id):
        """Confirm a draft via a durable pending -> confirming -> confirmed state machine.

        A crash or dropped response at any point is safe to retry: the deterministic match key
        is pinned to the draft before any workspace write, so a retry recognizes and reuses a
        listing this same draft already created instead of racing `workspace.add` into a
        spurious duplicate error, and guarded shared-pool promotion stays create-only via
        WorkspaceAutoSync's own sync-key idempotency.
        """
        with self._workspace(actor_id) as (workspace, record):
            draft = workspace.get_document_draft(actor_id, draft_id)
            if draft["status"] == "confirmed":
                return draft["outcome"]
            if draft["status"] not in {"pending", "confirming"}:
                raise ValueError("document draft is not pending")

            fields = dict(draft["candidates"])
            self._require_addition_fields(fields)
            validated = workspace.validate_public_fields(fields)
            key = compute_match_key(validated)
            if not key:
                raise ValueError("listing requires a stable address or MLS identity")

            if draft["status"] == "pending":
                if workspace.find(actor_id, key) is not None:
                    raise ValueError("workspace listing already exists")
                draft = workspace.begin_document_draft_confirmation(actor_id, draft_id, key)
            elif draft["match_key"] != key:
                raise ValueError("document draft identity changed unexpectedly")

            existing = workspace.find(actor_id, key)
            added = existing if existing is not None else workspace.add(actor_id, fields)
            outcome = {"record": added, "sync": self._sync_result(workspace, record, added)}
            workspace.complete_document_draft_confirmation(actor_id, draft_id, outcome)
            return outcome

    @staticmethod
    def _require_addition_fields(values):
        missing = [name for name in ("source_url", "observed_at") if not str(values.get(name) or "").strip()]
        if missing:
            raise ValueError(" and ".join(missing) + " are required")

    def _sync_result(self, workspace, record, added):
        if not record["auto_sync_additions"]:
            return {"status": "held_for_review", "code": "auto_sync_disabled",
                    "detail": "owner has disabled automatic shared-pool sync"}
        return self.autosync.sync_addition(workspace, added["match_key"], workspace_key=record["workspace_key"])

    def _attachments(self, workspace, record):
        evidence_root = Path(self.workspaces.root).parent / "contributor-workspace-attachments" / record["workspace_key"]
        return WorkspaceAttachmentService(workspace, quarantine_dir=evidence_root)

    def _open(self, actor_id):
        record = self.workspaces.for_contributor(actor_id, require_enabled=True)
        return WorkspaceStore(record["path"], contributor_id=actor_id), record

    @contextmanager
    def _workspace(self, actor_id):
        workspace, record = self._open(actor_id)
        try:
            yield workspace, record
        finally:
            workspace.close()


class MineCommands:
    """Never interpret `/mine` with an LLM; accept only structured workspace actions."""

    def __init__(self, service):
        self.service = service

    def try_handle(self, actor_id, text):
        raw = (text or "").strip()
        lines = raw.splitlines()
        if not lines:
            return None
        first = lines[0].split()
        if not first or first[0].lower().split("@", 1)[0] != "/mine":
            return None
        if len(first) == 1 or first[1].lower() in {"help", "template"}:
            return _HELP
        action = first[1].lower()
        try:
            if action == "list" and len(first) == 2:
                return self._list(actor_id)
            if action == "show" and len(first) >= 3:
                return self._show(actor_id, " ".join(first[2:]))
            if action == "add" and len(first) == 2:
                return self._add(actor_id, self._fields(lines[1:]))
            if action == "edit" and len(first) >= 3:
                return self._edit(actor_id, " ".join(first[2:]), self._fields(lines[1:]))
            if action in {"retire", "restore", "delete"} and len(first) >= 3:
                return self._lifecycle(actor_id, action, " ".join(first[2:]), self._reason(lines[1:]))
            if action == "draft" and len(first) == 3:
                return self._draft(actor_id, first[2])
            if action == "draft" and len(first) == 4 and first[2].lower() == "show":
                return self._show_draft(actor_id, first[3])
            if action == "draft" and len(first) == 4 and first[2].lower() == "edit":
                return self._edit_draft(actor_id, first[3], self._fields(lines[1:]))
            if action == "drafts" and len(first) == 2:
                return self._drafts(actor_id)
            if action == "confirm" and len(first) == 3:
                return self._confirm_draft(actor_id, first[2])
            if action == "discard" and len(first) == 3:
                return self._discard_draft(actor_id, first[2])
        except PermissionError:
            return "An enabled contributor workspace is required."
        except ValueError as exc:
            prefix = "Addition" if action == "add" else "Workspace change"
            return f"{prefix} was not accepted: {exc}. Send /mine help for the template."
        return ("Use /mine help, /mine list, /mine show <listing key>, /mine add, /mine edit, /mine retire, "
                "/mine restore, /mine delete, /mine draft, /mine drafts, /mine confirm, or /mine discard.")

    def _list(self, actor_id):
        records = self.service.list(actor_id)
        if not records:
            return "Your workspace has no listings. Send /mine add using /mine help."
        return "Your workspace listings:\n" + "\n".join(
            f"- {record['match_key']} | {record.get('street_address') or 'address unavailable'} | {record['status']}"
            for record in records
        )

    def _show(self, actor_id, match_key):
        record = self.service.show(actor_id, match_key)
        return "\n".join((
            "Your workspace listing:",
            f"Address: {record.get('street_address') or 'unavailable'}",
            f"ZIP: {record.get('zip') or 'unavailable'}",
            f"Price: {record.get('list_price') or 'unavailable'}",
            f"Status: {record['status']}",
            f"Observed: {record.get('observed_at') or 'unavailable'}",
            f"Source: {record.get('source_url') or 'unavailable'}",
        ))

    def _add(self, actor_id, fields):
        result = self.service.add(actor_id, fields)
        return self._addition_response(result)

    @staticmethod
    def _addition_response(result):
        sync = result["sync"]
        return f"Added to your workspace. Sync status: {sync['status']} ({sync['code']}). {sync['detail']}"

    def _edit(self, actor_id, match_key, fields):
        self.service.edit(actor_id, match_key, fields)
        return "Updated in your workspace. PENDING OWNER REVIEW. Shared pool was not changed."

    def _lifecycle(self, actor_id, action, match_key, reason):
        getattr(self.service, action)(actor_id, match_key, reason=reason)
        if action == "delete":
            return "Deleted from your workspace. PENDING OWNER REVIEW. Shared pool was not changed."
        verb = {"retire": "Retired", "restore": "Restored"}[action]
        return f"{verb} in your workspace. PENDING OWNER REVIEW. Shared pool was not changed."

    def _draft(self, actor_id, attachment_id):
        draft = self.service.create_draft_from_attachment(actor_id, self._positive_id(attachment_id))
        return (f"Draft {draft['id']} created from attachment {draft['attachment_id']}. Review it with /mine drafts, "
                f"then use /mine confirm {draft['id']} or /mine discard {draft['id']}.")

    def _show_draft(self, actor_id, draft_id):
        draft = self.service.show_draft(actor_id, self._positive_id(draft_id))
        fields = "\n".join(f"{key}: {value}" for key, value in sorted(draft["candidates"].items()))
        return (f"Draft {draft['id']} candidate fields (attachment {draft['attachment_id']}):\n{fields}\n"
                f"Review complete, then use /mine confirm {draft['id']} or /mine discard {draft['id']}.")

    def _edit_draft(self, actor_id, draft_id, fields):
        draft = self.service.edit_draft(actor_id, self._positive_id(draft_id), fields)
        return (f"Draft {draft['id']} updated. Review with /mine draft show {draft['id']}, "
                f"then use /mine confirm {draft['id']} or /mine discard {draft['id']}.")

    def _drafts(self, actor_id):
        drafts = self.service.list_drafts(actor_id)
        if not drafts:
            return "You have no pending document drafts."
        return ("Your pending document drafts:\n" + "\n".join(
            f"- Draft {draft['id']} from attachment {draft['attachment_id']}" for draft in drafts
        ) + "\nConfirm with /mine confirm <draft id> or discard with /mine discard <draft id>.")

    def _confirm_draft(self, actor_id, draft_id):
        draft_id = self._positive_id(draft_id)
        result = self.service.confirm_draft(actor_id, draft_id)
        return f"Draft {draft_id} confirmed. {self._addition_response(result)}"

    def _discard_draft(self, actor_id, draft_id):
        draft_id = self._positive_id(draft_id)
        self.service.discard_draft(actor_id, draft_id)
        return f"Draft {draft_id} discarded."

    @staticmethod
    def _positive_id(value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ValueError("a positive numeric ID is required") from None
        if parsed < 1:
            raise ValueError("a positive numeric ID is required")
        return parsed

    @staticmethod
    def _fields(lines):
        values = {}
        for line in lines:
            if not line.strip():
                continue
            if ":" not in line:
                raise ValueError("each listing line must be key: value")
            key, value = line.split(":", 1)
            key = key.strip().lower().replace(" ", "_")
            value = value.strip()
            if not key or not value or key in values:
                raise ValueError("listing fields must be non-empty and unique")
            values[key] = value
        if not values:
            raise ValueError("listing fields are required")
        return values

    @staticmethod
    def _reason(lines):
        values = MineCommands._fields(lines)
        if set(values) != {"reason"}:
            raise ValueError("reason is required")
        return values["reason"]
