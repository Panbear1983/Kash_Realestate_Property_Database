#!/usr/bin/env python3
"""Isolated contributor workspace record-store tests."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.contributor_workspace_store import WorkspaceStore  # noqa: E402

CONTRIBUTOR = 2
VALID = {
    "street_address": "10 Example Road",
    "zip": "10301",
    "list_price": 700000,
    "status": "active",
    "source_url": "https://example.test/listing/10",
    "observed_at": "2026-08-07",
}


def test_workspace_add_get_update_retire_and_restore_keep_audit_history():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = WorkspaceStore(Path(tmpdir) / "workspace.sqlite3", contributor_id=CONTRIBUTOR)
        created = workspace.add(CONTRIBUTOR, VALID)
        assert created["revision"] == 1 and created["status"] == "active"
        assert workspace.get(CONTRIBUTOR, created["match_key"])["list_price"] == 700000
        updated = workspace.update(CONTRIBUTOR, created["match_key"], {"list_price": 695000})
        assert updated["revision"] == 2 and updated["list_price"] == 695000
        retired = workspace.retire(CONTRIBUTOR, created["match_key"], reason="owner request")
        assert retired["status"] == "off_market"
        restored = workspace.restore(CONTRIBUTOR, created["match_key"], reason="listing restored")
        assert restored["status"] == "active"
        assert [event["event"] for event in workspace.audit(CONTRIBUTOR, created["match_key"])] == [
            "add", "update", "retire", "restore"
        ]


def test_workspace_rejects_shared_private_fields_and_cross_contributor_access():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = WorkspaceStore(Path(tmpdir) / "workspace.sqlite3", contributor_id=CONTRIBUTOR)
        bad = dict(VALID, my_notes="do not expose")
        try:
            workspace.add(CONTRIBUTOR, bad)
        except ValueError as exc:
            assert "not permitted" in str(exc)
        else:
            raise AssertionError("workspace accepted a shared private field")
        created = workspace.add(CONTRIBUTOR, VALID)
        try:
            workspace.get(CONTRIBUTOR + 1, created["match_key"])
        except PermissionError:
            pass
        else:
            raise AssertionError("another contributor read this workspace")


def test_workspace_soft_delete_hides_record_and_preserves_audit_history():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = WorkspaceStore(Path(tmpdir) / "workspace.sqlite3", contributor_id=CONTRIBUTOR)
        created = workspace.add(CONTRIBUTOR, VALID)
        deleted = workspace.delete(CONTRIBUTOR, created["match_key"], reason="duplicate listing")
        assert deleted["revision"] == 2
        assert deleted["deleted_at"] is not None
        assert workspace.list(CONTRIBUTOR) == []
        assert workspace.list(CONTRIBUTOR, include_deleted=True)[0]["match_key"] == created["match_key"]
        assert [event["event"] for event in workspace.audit(CONTRIBUTOR, created["match_key"])] == ["add", "delete"]


def test_workspace_lifecycle_reasons_are_retained_in_audit():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = WorkspaceStore(Path(tmpdir) / "workspace.sqlite3", contributor_id=CONTRIBUTOR)
        created = workspace.add(CONTRIBUTOR, VALID)
        workspace.retire(CONTRIBUTOR, created["match_key"], reason="seller withdrew listing")
        workspace.restore(CONTRIBUTOR, created["match_key"], reason="seller relisted")
        workspace.delete(CONTRIBUTOR, created["match_key"], reason="duplicate workspace record")
        audit = workspace.audit(CONTRIBUTOR, created["match_key"])
        assert [(event["event"], event["reason"]) for event in audit[1:]] == [
            ("retire", "seller withdrew listing"),
            ("restore", "seller relisted"),
            ("delete", "duplicate workspace record"),
        ]


def test_workspace_restore_reinstates_a_soft_deleted_record():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = WorkspaceStore(Path(tmpdir) / "workspace.sqlite3", contributor_id=CONTRIBUTOR)
        created = workspace.add(CONTRIBUTOR, VALID)
        workspace.delete(CONTRIBUTOR, created["match_key"], reason="submitted in error")
        restored = workspace.restore(CONTRIBUTOR, created["match_key"], reason="retain corrected record")
        assert restored["status"] == "active"
        assert restored["deleted_at"] is None
        assert workspace.list(CONTRIBUTOR)[0]["match_key"] == created["match_key"]


def test_workspace_rejects_nonpublic_or_credential_bearing_source_urls():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = WorkspaceStore(Path(tmpdir) / "workspace.sqlite3", contributor_id=CONTRIBUTOR)
        for url in ("https://localhost/listing", "https://127.0.0.1/listing", "https://user:secret@example.test/listing"):
            try:
                workspace.add(CONTRIBUTOR, {**VALID, "source_url": url})
            except ValueError as exc:
                assert "public HTTPS" in str(exc)
            else:
                raise AssertionError(f"unsafe source URL accepted: {url}")


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"{len(tests)} passed — workspace records are isolated and audited")
