#!/usr/bin/env python3
"""Guarded promotion of new contributor workspace records into the shared pool."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.contributor_autosync import WorkspaceAutoSync  # noqa: E402
from kash.contributor_workspace_store import WorkspaceStore  # noqa: E402
from kash.store import Store  # noqa: E402

CONTRIBUTOR = 2
VALID = {
    "street_address": "10 Example Road", "zip": "10301", "list_price": 700000,
    "status": "active", "source_url": "https://example.test/listing/10",
    "observed_at": "2026-08-07",
}


def test_clean_new_workspace_listing_syncs_once_with_provenance():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = WorkspaceStore(Path(tmpdir) / "workspace.sqlite3", contributor_id=CONTRIBUTOR)
        record = workspace.add(CONTRIBUTOR, VALID)
        shared = Store(":memory:")
        sync = WorkspaceAutoSync(shared)
        result = sync.sync_addition(workspace, record["match_key"], workspace_key="opaque-key")
        assert result["status"] == "synced"
        assert shared.get(record["match_key"])["list_price"] == 700000
        assert sync.sync_addition(workspace, record["match_key"], workspace_key="opaque-key")["status"] == "synced"
        assert len(shared.conn.execute("SELECT * FROM contributor_workspace_sync_audit").fetchall()) == 1


def test_existing_shared_listing_is_held_not_overwritten():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = WorkspaceStore(Path(tmpdir) / "workspace.sqlite3", contributor_id=CONTRIBUTOR)
        record = workspace.add(CONTRIBUTOR, VALID)
        shared = Store(":memory:")
        shared.upsert({**VALID, "list_price": 900000}, source="existing")
        result = WorkspaceAutoSync(shared).sync_addition(workspace, record["match_key"], workspace_key="opaque-key")
        assert result["status"] == "held_for_review"
        assert result["code"] == "duplicate_shared_match_key"
        assert shared.get(record["match_key"])["list_price"] == 900000


def test_missing_observed_date_is_held_for_owner_review():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = WorkspaceStore(Path(tmpdir) / "workspace.sqlite3", contributor_id=CONTRIBUTOR)
        record = workspace.add(CONTRIBUTOR, {key: value for key, value in VALID.items() if key != "observed_at"})
        result = WorkspaceAutoSync(Store(":memory:")).sync_addition(workspace, record["match_key"], workspace_key="opaque-key")
        assert result["status"] == "held_for_review"
        assert result["code"] == "missing_observed_at"


def test_shared_store_create_only_refuses_existing_identity_without_overwrite():
    shared = Store(":memory:")
    assert shared.insert_new_only(VALID, source="first") is True
    assert shared.insert_new_only({**VALID, "list_price": 900000}, source="second") is False
    assert shared.get("addr:10 example rd|10301")["list_price"] == 700000


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"{len(tests)} passed — contributor additions sync only when guarded")
