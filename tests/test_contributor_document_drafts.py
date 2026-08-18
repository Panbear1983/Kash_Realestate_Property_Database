#!/usr/bin/env python3
"""Private contributor document-draft persistence and bounded extraction tests."""
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docx import Document  # noqa: E402
from kash.contributor_document_drafts import (  # noqa: E402
    MAX_DOCX_INPUT_BYTES,
    MAX_DOCX_MEMBER_COUNT,
    MAX_DOCX_MEMBER_UNCOMPRESSED_BYTES,
    MAX_DOCX_TOTAL_UNCOMPRESSED_BYTES,
    _docx_archive_preflight,
    extract_document_candidates,
)
from kash.contributor_workspace_attachments import WorkspaceAttachmentService  # noqa: E402
from kash.contributor_workspace_service import ContributorWorkspaceService  # noqa: E402
from kash.contributor_workspace_store import WorkspaceStore  # noqa: E402
from kash.contributor_autosync import WorkspaceAutoSync  # noqa: E402
from kash.contributor_workspaces import ContributorWorkspaces  # noqa: E402
from kash.data_roles import DataRoles, ROLE_CONTRIBUTOR  # noqa: E402
from kash.store import Store  # noqa: E402

CONTRIBUTOR, OTHER = 2, 3
VALID = {
    "street_address": "10 Example Road",
    "zip": "10301",
    "list_price": 700000,
    "status": "active",
    "source_url": "https://example.test/listing/10",
    "observed_at": "2026-08-07",
}
DOCX = b"PK\x03\x04document fixture"


def _docx(path, *lines):
    document = Document()
    for line in lines:
        document.add_paragraph(line)
    document.save(path)


def _service(tmpdir):
    shared = Store(":memory:")
    roles = DataRoles(shared)
    roles.bootstrap_owner(1)
    roles.grant(1, CONTRIBUTOR, ROLE_CONTRIBUTOR, reason="document draft test")
    workspaces = ContributorWorkspaces(shared, roles, root=Path(tmpdir))
    workspaces.create(1, CONTRIBUTOR, reason="document draft test")
    workspaces.set_auto_sync(1, CONTRIBUTOR, enabled=True, reason="test opt-in")
    return ContributorWorkspaceService(workspaces, WorkspaceAutoSync(shared))


def _document_bytes(tmpdir, *lines):
    path = Path(tmpdir) / "source.docx"
    _docx(path, *lines)
    return path.read_bytes()


def test_owner_can_create_list_and_discard_attachment_linked_draft_but_other_contributor_cannot_read_it():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = WorkspaceStore(Path(tmpdir) / "workspace.sqlite3", contributor_id=CONTRIBUTOR)
        listing = workspace.add(CONTRIBUTOR, VALID)
        attachment = WorkspaceAttachmentService(workspace, quarantine_dir=Path(tmpdir) / "evidence").stage_mine(
            CONTRIBUTOR, listing["match_key"], "listing.docx", DOCX
        )

        draft = workspace.create_document_draft(
            CONTRIBUTOR, attachment["id"], {"street_address": "20 Draft Lane", "zip": "10302"}
        )

        assert draft["attachment_id"] == attachment["id"]
        assert draft["status"] == "pending"
        assert workspace.list_document_drafts(CONTRIBUTOR) == [draft]
        try:
            workspace.list_document_drafts(OTHER)
        except PermissionError:
            pass
        else:
            raise AssertionError("another contributor read document drafts")
        discarded = workspace.discard_document_draft(CONTRIBUTOR, draft["id"])
        assert discarded["status"] == "discarded"
        assert workspace.list_document_drafts(CONTRIBUTOR) == []


def test_docx_extraction_returns_only_allow_list_field_value_candidates():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "listing.docx"
        _docx(
            path,
            "street_address: 20 Draft Lane",
            "zip: 10302",
            "list_price: 725000",
            "source_url: https://example.test/listing/20",
            "observed_at: 2026-08-07",
            "ignore this prose and unrecognized_field: do not retain",
        )

        assert extract_document_candidates(path, "docx") == {
            "street_address": "20 Draft Lane", "zip": "10302", "list_price": "725000",
            "source_url": "https://example.test/listing/20", "observed_at": "2026-08-07",
        }


def test_document_extraction_safely_rejects_malformed_oversize_and_unrecognized_input():
    with tempfile.TemporaryDirectory() as tmpdir:
        malformed = Path(tmpdir) / "malformed.docx"
        malformed.write_bytes(b"PK\x03\x04not a document")
        oversized = Path(tmpdir) / "oversized.docx"
        _docx(oversized, "street_address: " + "x" * 70000)
        unrecognized = Path(tmpdir) / "unrecognized.docx"
        _docx(unrecognized, "not a field: value", "my_notes: private")

        assert extract_document_candidates(malformed, "docx") == {}
        assert extract_document_candidates(oversized, "docx") == {}
        assert extract_document_candidates(unrecognized, "docx") == {}


def _fake_member(name="part.xml", file_size=100, compress_size=100, flag_bits=0):
    info = zipfile.ZipInfo(name)
    info.file_size = file_size
    info.compress_size = compress_size
    info.flag_bits = flag_bits
    return info


class _FakeZipFile:
    def __init__(self, members):
        self._members = members

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def infolist(self):
        return self._members


def _patched_zipfile(members):
    return patch("kash.contributor_document_drafts.zipfile.ZipFile", lambda path: _FakeZipFile(members))


def test_docx_preflight_accepts_a_benign_small_archive():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "listing.docx"
        _docx(path, "street_address: 60 Preflight Way")

        assert _docx_archive_preflight(path) is True


def test_docx_preflight_rejects_non_regular_and_oversized_input():
    with tempfile.TemporaryDirectory() as tmpdir:
        directory = Path(tmpdir) / "not_a_file.docx"
        directory.mkdir()
        oversized = Path(tmpdir) / "huge.docx"
        oversized.write_bytes(b"0" * (MAX_DOCX_INPUT_BYTES + 1))

        assert _docx_archive_preflight(directory) is False
        assert _docx_archive_preflight(oversized) is False
        assert extract_document_candidates(directory, "docx") == {}
        assert extract_document_candidates(oversized, "docx") == {}


def test_docx_preflight_rejects_dangerous_archive_metadata():
    with tempfile.TemporaryDirectory() as tmpdir:
        placeholder = Path(tmpdir) / "placeholder.docx"
        placeholder.write_bytes(b"PK\x03\x04placeholder")

        encrypted = [_fake_member(flag_bits=0x1)]
        too_many_members = [_fake_member(name=f"part{i}.xml") for i in range(MAX_DOCX_MEMBER_COUNT + 1)]
        overlarge_member = [_fake_member(
            file_size=MAX_DOCX_MEMBER_UNCOMPRESSED_BYTES + 1, compress_size=MAX_DOCX_MEMBER_UNCOMPRESSED_BYTES + 1,
        )]
        excessive_aggregate = [
            _fake_member(name=f"big{i}.xml", file_size=30 * 1024 * 1024, compress_size=30 * 1024 * 1024)
            for i in range(3)
        ]
        assert 3 * 30 * 1024 * 1024 > MAX_DOCX_TOTAL_UNCOMPRESSED_BYTES
        suspicious_expansion = [_fake_member(file_size=5 * 1024 * 1024, compress_size=1024)]

        for members in (encrypted, too_many_members, overlarge_member, excessive_aggregate, suspicious_expansion):
            with _patched_zipfile(members):
                assert _docx_archive_preflight(placeholder) is False


def test_dangerous_archive_metadata_is_rejected_before_python_docx_opens_it():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "bomb.docx"
        path.write_bytes(b"PK\x03\x04placeholder")
        bomb_members = [_fake_member(file_size=5 * 1024 * 1024, compress_size=1024)]

        def guard(*args, **kwargs):
            raise AssertionError("python-docx must not open a rejected archive")

        with _patched_zipfile(bomb_members), patch("kash.contributor_document_drafts.Document", guard):
            assert extract_document_candidates(path, "docx") == {}


def test_legacy_doc_extraction_uses_textutil_without_shell_on_a_read_only_temporary_copy():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "listing.doc"
        path.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"legacy fixture")
        calls = []

        def runner(command, **kwargs):
            copied_path = Path(command[-1])
            calls.append((command, kwargs, copied_path, copied_path.stat().st_mode & 0o222))
            return subprocess.CompletedProcess(command, 0, "street_address: 30 Legacy Road\nzip: 10303\n", "")

        assert extract_document_candidates(path, "doc", runner=runner) == {
            "street_address": "30 Legacy Road", "zip": "10303"
        }
        command, kwargs, copied_path, writable_bits = calls[0]
        assert command[:4] == ["/usr/bin/textutil", "-convert", "txt", "-stdout"]
        assert kwargs["shell"] is False and kwargs["timeout"] == 5
        assert copied_path != path and writable_bits == 0 and not copied_path.exists()


def test_amending_pending_document_draft_merges_allowed_fields_and_enforces_owner_and_status():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = WorkspaceStore(Path(tmpdir) / "workspace.sqlite3", contributor_id=CONTRIBUTOR)
        listing = workspace.add(CONTRIBUTOR, VALID)
        attachment = WorkspaceAttachmentService(workspace, quarantine_dir=Path(tmpdir) / "evidence").stage_mine(
            CONTRIBUTOR, listing["match_key"], "listing.docx", DOCX
        )
        draft = workspace.create_document_draft(
            CONTRIBUTOR, attachment["id"], {"street_address": "20 Draft Lane", "zip": "10302"}
        )

        amended = workspace.update_document_draft(CONTRIBUTOR, draft["id"], {"zip": "10399", "list_price": "725000"})
        assert amended["candidates"] == {
            "street_address": "20 Draft Lane", "zip": "10399", "list_price": "725000",
        }
        assert workspace.get_document_draft(CONTRIBUTOR, draft["id"])["candidates"] == amended["candidates"]

        try:
            workspace.update_document_draft(CONTRIBUTOR, draft["id"], {"my_notes": "private"})
        except ValueError as exc:
            assert "my_notes" in str(exc)
        else:
            raise AssertionError("a disallowed field was amended into a document draft")

        try:
            workspace.update_document_draft(OTHER, draft["id"], {"zip": "10400"})
        except PermissionError:
            pass
        else:
            raise AssertionError("another contributor amended a document draft")

        workspace.discard_document_draft(CONTRIBUTOR, draft["id"])
        try:
            workspace.update_document_draft(CONTRIBUTOR, draft["id"], {"zip": "10401"})
        except ValueError as exc:
            assert "not pending" in str(exc)
        else:
            raise AssertionError("a discarded document draft was amended")


def test_confirming_document_draft_without_source_or_date_fails_without_another_workspace_listing():
    with tempfile.TemporaryDirectory() as tmpdir:
        service = _service(tmpdir)
        seed = service.add(CONTRIBUTOR, VALID)["record"]
        attachment = service.attach(
            CONTRIBUTOR, seed["match_key"], "draft.docx",
            _document_bytes(tmpdir, "street_address: 40 Incomplete Road", "zip: 10304"),
        )
        draft = service.create_draft_from_attachment(CONTRIBUTOR, attachment["id"])

        try:
            service.confirm_draft(CONTRIBUTOR, draft["id"])
        except ValueError as exc:
            assert "source_url" in str(exc) and "observed_at" in str(exc)
        else:
            raise AssertionError("incomplete document draft was confirmed")
        assert len(service.list(CONTRIBUTOR)) == 1


def test_confirming_complete_document_draft_uses_workspace_add_and_returns_guarded_sync_status():
    with tempfile.TemporaryDirectory() as tmpdir:
        service = _service(tmpdir)
        seed = service.add(CONTRIBUTOR, VALID)["record"]
        attachment = service.attach(
            CONTRIBUTOR, seed["match_key"], "draft.docx", _document_bytes(
                tmpdir, "street_address: 50 Confirmed Road", "zip: 10305", "list_price: 750000",
                "status: active", "source_url: https://example.test/listing/50", "observed_at: 2026-08-07",
            ),
        )
        draft = service.create_draft_from_attachment(CONTRIBUTOR, attachment["id"])

        result = service.confirm_draft(CONTRIBUTOR, draft["id"])

        assert result["record"]["street_address"] == "50 Confirmed Road"
        assert result["sync"]["status"] == "synced"
        assert len(service.list(CONTRIBUTOR)) == 2
        assert service.list_drafts(CONTRIBUTOR) == []


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"{len(tests)} passed — document drafts stay contributor-private")
