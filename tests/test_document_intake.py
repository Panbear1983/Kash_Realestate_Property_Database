#!/usr/bin/env python3
"""End-to-end document intake: upload → draft preview → /mine submit → pending proposal
with evidence → owner review → pool change → pings. Fully offline (fake backend, recorder
push, temp dirs); the shared pool is only ever written by publish."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docx import Document                                        # noqa: E402
from kash.contribution_attachments import AttachmentService      # noqa: E402
from kash.contribution_notifications import ProposalNotifier     # noqa: E402
from kash.contributions import ContributionService               # noqa: E402
from kash.contributor_autosync import WorkspaceAutoSync          # noqa: E402
from kash.contributor_workspace_service import ContributorWorkspaceService  # noqa: E402
from kash.contributor_workspaces import ContributorWorkspaces    # noqa: E402
from kash.data_roles import DataRoles, ROLE_CONTRIBUTOR          # noqa: E402
from kash.document_intake import DocumentIntakeService, IntakeCommands  # noqa: E402
from kash.store import Store                                     # noqa: E402

OWNER, CONTRIBUTOR = 1, 2


class FakeBackend:
    name = chosen = "fake"
    last_usage = None

    def __init__(self, reply=None):
        self.reply = reply
        self.calls = []

    def available(self):
        return True, ""

    def query_spec(self, prompt, schema):
        self.calls.append(prompt)
        if self.reply is None:
            raise RuntimeError("ladder down")
        base = {name: None for name in schema["required"]}
        base.update(self.reply)
        return base


class RecorderPush:
    def __init__(self):
        self.sent = []

    def __call__(self, text, chat_ids):
        self.sent.append((tuple(int(c) for c in chat_ids), text))
        return {int(uid): "sent" for uid in chat_ids}


class Rig:
    def __init__(self, tmpdir):
        self.store = Store(":memory:")
        self.roles = DataRoles(self.store)
        self.roles.bootstrap_owner(OWNER)
        self.roles.grant(OWNER, CONTRIBUTOR, ROLE_CONTRIBUTOR, reason="intake test")
        workspaces = ContributorWorkspaces(self.store, self.roles,
                                           root=Path(tmpdir) / "workspaces")
        workspaces.create(OWNER, CONTRIBUTOR, reason="intake test")
        self.workspace_service = ContributorWorkspaceService(
            workspaces, WorkspaceAutoSync(self.store))
        self.contributions = ContributionService(self.store, self.roles)
        self.attachments = AttachmentService(
            self.store, self.roles, quarantine_dir=Path(tmpdir) / "proposal-evidence")
        self.push = RecorderPush()
        self.intake = DocumentIntakeService(
            self.workspace_service, self.contributions, self.attachments, self.store,
            notifier=ProposalNotifier(self.store, self.roles, self.push))
        self.commands = IntakeCommands(self.intake)


def _docx_bytes(tmpdir, *lines):
    path = Path(tmpdir) / "sheet.docx"
    document = Document()
    for line in lines:
        document.add_paragraph(line)
    document.save(path)
    return path.read_bytes()


def _draft_id(reply: str) -> int:
    for token in reply.replace(",", " ").replace(".", " ").split():
        if token.isdigit():
            pass
    import re
    return int(re.search(r"draft (\d+)", reply).group(1))


ADD_REPLY = {"street_address": "123 Example Rd", "zip": "10301", "list_price": 700000,
             "beds": "3", "status": "active",
             "source_url": "https://example.test/listing/123",
             "observed_at": "2026-08-25"}


def test_add_flow_end_to_end_with_evidence_ping_and_publish():
    with tempfile.TemporaryDirectory() as tmpdir:
        rig = Rig(tmpdir)
        content = _docx_bytes(tmpdir, "Lovely home at 123 Example Rd, asking 700k")
        preview = rig.intake.intake_document(
            CONTRIBUTOR, "sheet.docx", content, backend=FakeBackend(ADD_REPLY))
        assert "would submit an ADD" in preview
        assert "street_address: 123 Example Rd" in preview
        assert "Nothing has been submitted yet" in preview
        did = _draft_id(preview)

        reply = rig.commands.try_handle(CONTRIBUTOR, f"/mine submit {did}")
        assert "pending owner review" in reply and "has not changed any listing" in reply

        row = rig.store.conn.execute(
            "SELECT id, kind, status, submitted_by FROM listing_proposals").fetchone()
        assert row[1] == "add" and row[2] == "pending" and row[3] == CONTRIBUTOR
        pid = int(row[0])

        # evidence copied into the proposal quarantine with the same digest
        evidence = rig.store.conn.execute(
            "SELECT proposal_id, sha256 FROM contribution_attachments").fetchone()
        assert evidence[0] == pid
        import hashlib
        assert evidence[1] == hashlib.sha256(content).hexdigest()

        # owner pinged exactly once, even across a retried submit
        assert len(rig.push.sent) == 1 and rig.push.sent[0][0] == (OWNER,)
        assert "F3" in rig.push.sent[0][1]
        retry = rig.commands.try_handle(CONTRIBUTOR, f"/mine submit {did}")
        assert f"#{pid}" in retry
        assert len(rig.push.sent) == 1
        assert rig.store.conn.execute(
            "SELECT COUNT(*) FROM listing_proposals").fetchone()[0] == 1

        # the pool is untouched until the owner acts; then it changes
        assert rig.store.count() == 0
        rig.contributions.approve(OWNER, pid, reason="checked the source")
        rig.contributions.publish(OWNER, pid)
        assert rig.store.count() == 1
        published = rig.store.conn.execute(
            "SELECT source FROM listings").fetchone()[0]
        assert published == f"human-reviewed:{pid}"
        rig.store.close()


def test_correction_flow_shows_a_diff_and_submits_changed_fields_only():
    with tempfile.TemporaryDirectory() as tmpdir:
        rig = Rig(tmpdir)
        rig.store.upsert({"street_address": "123 Example Rd", "zip": "10301",
                          "list_price": 725000, "beds": "3", "status": "active"},
                         "fixture")
        content = _docx_bytes(tmpdir, "price reduced")
        preview = rig.intake.intake_document(
            CONTRIBUTOR, "sheet.docx", content, backend=FakeBackend(ADD_REPLY))
        assert "CORRECTION" in preview
        assert "list_price: 725000 -> 700000" in preview
        assert "beds" not in preview.split("CORRECTION")[1].split("Nothing")[0]
        did = _draft_id(preview)

        reply = rig.commands.try_handle(CONTRIBUTOR, f"/mine submit {did}")
        assert "correct" in reply
        import json
        payload = json.loads(rig.store.conn.execute(
            "SELECT payload_json FROM listing_proposals").fetchone()[0])
        assert payload["fields"] == {"list_price": 700000}
        assert payload["expected"] == {"list_price": 725000}
        rig.store.close()


def test_sold_document_for_a_tracked_listing_becomes_a_retire():
    with tempfile.TemporaryDirectory() as tmpdir:
        rig = Rig(tmpdir)
        rig.store.upsert({"street_address": "123 Example Rd", "zip": "10301",
                          "list_price": 725000, "status": "active"}, "fixture")
        sold = {**ADD_REPLY, "status": "sold", "sold_date": "2026-08-20"}
        preview = rig.intake.intake_document(
            CONTRIBUTOR, "sheet.docx", _docx_bytes(tmpdir, "SOLD"),
            backend=FakeBackend(sold))
        assert "RETIRE" in preview and "SOLD" in preview
        did = _draft_id(preview)
        rig.commands.try_handle(CONTRIBUTOR, f"/mine submit {did}")
        row = rig.store.conn.execute(
            "SELECT kind, payload_json FROM listing_proposals").fetchone()
        assert row[0] == "retire"
        assert "sold" in row[1] and "2026-08-20" in row[1]
        # publish actually retires
        import json
        pid = rig.store.conn.execute("SELECT id FROM listing_proposals").fetchone()[0]
        rig.contributions.approve(OWNER, pid, reason="confirmed sold")
        rig.contributions.publish(OWNER, pid)
        assert rig.store.conn.execute(
            "SELECT status FROM listings").fetchone()[0] == "off_market"
        rig.store.close()


def test_backend_failure_falls_back_to_literal_lines_and_missing_bits_block_submit():
    with tempfile.TemporaryDirectory() as tmpdir:
        rig = Rig(tmpdir)
        content = _docx_bytes(tmpdir, "street_address: 9 Fallback Ct", "zip: 10302")
        preview = rig.intake.intake_document(
            CONTRIBUTOR, "sheet.docx", content, backend=FakeBackend(None))  # raises
        assert "street_address: 9 Fallback Ct" in preview
        assert "Still needed before submitting" in preview and "source_url" in preview
        did = _draft_id(preview)
        reply = rig.commands.try_handle(CONTRIBUTOR, f"/mine submit {did}")
        assert "not accepted" in reply and "source_url" in reply
        assert rig.store.conn.execute(
            "SELECT COUNT(*) FROM listing_proposals").fetchone()[0] == 0
        # fixing it via the existing draft edit verb unblocks the submit
        rig.workspace_service.edit_draft(CONTRIBUTOR, did, {
            "source_url": "https://example.test/fallback/9", "list_price": "600000"})
        reply = rig.commands.try_handle(CONTRIBUTOR, f"/mine submit {did}")
        assert "pending owner review" in reply
        rig.store.close()


def test_image_with_empty_caption_becomes_evidence_plus_typed_draft_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        rig = Rig(tmpdir)
        png = b"\x89PNG\r\n\x1a\n screenshot fixture"
        reply = rig.intake.intake_document(CONTRIBUTOR, "shot.png", png,
                                           backend=FakeBackend({}))
        assert "can't read text out of an image" in reply
        assert "/mine draft edit" in reply
        rig.store.close()


def test_image_caption_alone_can_drive_a_retire():
    with tempfile.TemporaryDirectory() as tmpdir:
        rig = Rig(tmpdir)
        rig.store.upsert({"street_address": "123 Example Rd", "zip": "10301",
                          "status": "active"}, "fixture")
        sold = {"street_address": "123 Example Rd", "zip": "10301", "status": "sold",
                "source_url": "https://example.test/sold/123",
                "observed_at": "2026-08-25"}
        reply = rig.intake.intake_document(
            CONTRIBUTOR, "shot.png", b"\x89PNG\r\n\x1a\n fixture",
            caption="sold — 123 Example Rd 10301", backend=FakeBackend(sold))
        assert "RETIRE" in reply
        rig.store.close()


def test_purged_evidence_stops_the_submit_with_a_reupload_message():
    with tempfile.TemporaryDirectory() as tmpdir:
        rig = Rig(tmpdir)
        preview = rig.intake.intake_document(
            CONTRIBUTOR, "sheet.docx", _docx_bytes(tmpdir, "x"),
            backend=FakeBackend(ADD_REPLY))
        did = _draft_id(preview)
        # simulate the 30-day purge deleting the workspace bytes
        root = Path(tmpdir) / "workspaces"
        for path in root.parent.rglob("*"):
            if path.is_file() and path.suffix == ".docx":
                path.unlink()
        reply = rig.commands.try_handle(CONTRIBUTOR, f"/mine submit {did}")
        assert "not accepted" in reply
        assert rig.store.conn.execute(
            "SELECT COUNT(*) FROM listing_proposals").fetchone()[0] == 0
        rig.store.close()


def test_other_mine_verbs_fall_through_untouched():
    with tempfile.TemporaryDirectory() as tmpdir:
        rig = Rig(tmpdir)
        assert rig.commands.try_handle(CONTRIBUTOR, "/mine drafts") is None
        assert rig.commands.try_handle(CONTRIBUTOR, "/mine confirm 3") is None
        assert rig.commands.try_handle(CONTRIBUTOR, "hello") is None
        assert "draft id" in rig.commands.try_handle(CONTRIBUTOR, "/mine submit")
        assert "draft id" in rig.commands.try_handle(CONTRIBUTOR, "/mine submit abc")
        rig.store.close()


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — a document becomes a reviewed proposal, never a row")
