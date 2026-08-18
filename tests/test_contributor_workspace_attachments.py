#!/usr/bin/env python3
"""Workspace evidence remains quarantined and cannot affect listing payloads."""
import hashlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.contributor_workspace_attachments import WorkspaceAttachmentService  # noqa: E402
from kash.contributor_workspace_store import WorkspaceStore  # noqa: E402

CONTRIBUTOR, OTHER_CONTRIBUTOR = 2, 3
PDF = b"%PDF-1.7\nworkspace evidence fixture"
LEGACY_DOC = bytes.fromhex("D0CF11E0A1B11AE1") + b"legacy workspace evidence"
VALID = {
    "street_address": "10 Example Road",
    "zip": "10301",
    "list_price": 700000,
    "status": "active",
    "source_url": "https://example.test/listing/10",
    "observed_at": "2026-08-07",
}


def test_workspace_owner_can_attach_pdf_to_active_listing_without_mutating_payload():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = WorkspaceStore(Path(tmpdir) / "workspace.sqlite3", contributor_id=CONTRIBUTOR)
        listing = workspace.add(CONTRIBUTOR, VALID)
        before = workspace.get(CONTRIBUTOR, listing["match_key"])
        attachments = WorkspaceAttachmentService(workspace, quarantine_dir=Path(tmpdir) / "evidence")

        record = attachments.stage_mine(CONTRIBUTOR, listing["match_key"], "inspection.pdf", PDF)

        assert record["workspace_match_key"] == listing["match_key"]
        assert record["uploaded_by"] == CONTRIBUTOR
        assert record["status"] == "quarantined"
        assert record["extraction"] == "disabled"
        assert record["sha256"] == hashlib.sha256(PDF).hexdigest()
        assert Path(record["path"]).parent == Path(tmpdir) / "evidence"
        assert Path(record["path"]).name != "inspection.pdf"
        assert workspace.get(CONTRIBUTOR, listing["match_key"]) == before
        audit = workspace.conn.execute(
            "SELECT workspace_match_key,uploaded_by,sha256,media_kind,byte_size,status,extraction "
            "FROM workspace_attachments"
        ).fetchone()
        assert tuple(audit) == (listing["match_key"], CONTRIBUTOR, hashlib.sha256(PDF).hexdigest(),
                                "pdf", len(PDF), "quarantined", "disabled")


def test_workspace_attachment_rejects_nonowner_and_nonactive_listing_before_storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = WorkspaceStore(Path(tmpdir) / "workspace.sqlite3", contributor_id=CONTRIBUTOR)
        listing = workspace.add(CONTRIBUTOR, VALID)
        attachments = WorkspaceAttachmentService(workspace, quarantine_dir=Path(tmpdir) / "evidence")
        for actor_id, key in ((OTHER_CONTRIBUTOR, listing["match_key"]),):
            try:
                attachments.stage_mine(actor_id, key, "inspection.pdf", PDF)
            except PermissionError:
                pass
            else:
                raise AssertionError("nonowner attached workspace evidence")
        workspace.retire(CONTRIBUTOR, listing["match_key"], reason="no longer active")
        try:
            attachments.stage_mine(CONTRIBUTOR, listing["match_key"], "inspection.pdf", PDF)
        except ValueError as exc:
            assert "active" in str(exc)
        else:
            raise AssertionError("retired workspace listing accepted evidence")
        assert not (Path(tmpdir) / "evidence").exists()


def test_workspace_attachment_keeps_existing_type_magic_size_and_manual_review_guards():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = WorkspaceStore(Path(tmpdir) / "workspace.sqlite3", contributor_id=CONTRIBUTOR)
        listing = workspace.add(CONTRIBUTOR, VALID)
        evidence_dir = Path(tmpdir) / "evidence"
        attachments = WorkspaceAttachmentService(workspace, quarantine_dir=evidence_dir)
        manual = attachments.stage_mine(CONTRIBUTOR, listing["match_key"], "legacy.doc", LEGACY_DOC)
        assert manual["status"] == "manual_review"
        for filename, content in (("fake.pdf", b"not a PDF"), ("archive.zip", b"PK\x03\x04"),
                                  ("large.pdf", b"%PDF-" + b"x" * (10 * 1024 * 1024))):
            try:
                attachments.stage_mine(CONTRIBUTOR, listing["match_key"], filename, content)
            except ValueError:
                pass
            else:
                raise AssertionError(f"unsafe workspace attachment accepted: {filename}")
        assert [path.name for path in evidence_dir.iterdir()] == [Path(manual["path"]).name]


def test_expired_workspace_evidence_deletes_file_but_keeps_audit_metadata():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = WorkspaceStore(Path(tmpdir) / "workspace.sqlite3", contributor_id=CONTRIBUTOR)
        listing = workspace.add(CONTRIBUTOR, VALID)
        attachments = WorkspaceAttachmentService(workspace, quarantine_dir=Path(tmpdir) / "evidence")
        record = attachments.stage_mine(CONTRIBUTOR, listing["match_key"], "inspection.pdf", PDF)
        workspace.conn.execute("UPDATE workspace_attachments SET created_at='2000-01-01 00:00:00'")
        workspace.conn.commit()

        assert attachments.purge_expired() == 1
        assert not Path(record["path"]).exists()
        audit = workspace.conn.execute(
            "SELECT workspace_match_key,uploaded_by,sha256,status,extraction FROM workspace_attachments"
        ).fetchone()
        assert tuple(audit) == (listing["match_key"], CONTRIBUTOR, hashlib.sha256(PDF).hexdigest(),
                                "expired", "disabled")


def test_workspace_owner_can_upload_doc_as_unlinked_draft_input_before_any_listing_exists():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = WorkspaceStore(Path(tmpdir) / "workspace.sqlite3", contributor_id=CONTRIBUTOR)
        attachments = WorkspaceAttachmentService(workspace, quarantine_dir=Path(tmpdir) / "evidence")
        record = attachments.stage_mine(CONTRIBUTOR, None, "listing.doc", LEGACY_DOC)
        assert record["id"] > 0
        assert record["workspace_match_key"] is None
        assert record["status"] == "manual_review"


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"{len(tests)} passed — workspace evidence is owner-only and non-publishing")
