#!/usr/bin/env python3
"""Owner-managed contributor workspace registry tests."""
import tempfile
from pathlib import Path
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.contributor_workspaces import ContributorWorkspaces  # noqa: E402
from kash.data_roles import DataRoles, ROLE_CONTRIBUTOR  # noqa: E402
from kash.store import Store  # noqa: E402

OWNER, CONTRIBUTOR, READER = 1, 2, 3


def _setup(tmpdir):
    store = Store(":memory:")
    roles = DataRoles(store)
    roles.bootstrap_owner(OWNER)
    roles.grant(OWNER, CONTRIBUTOR, ROLE_CONTRIBUTOR, reason="approved workspace canary")
    return store, roles, ContributorWorkspaces(store, roles, root=Path(tmpdir))


def test_owner_creates_one_opaque_workspace_for_granted_contributor():
    with tempfile.TemporaryDirectory() as tmpdir:
        _store, _roles, workspaces = _setup(tmpdir)
        first = workspaces.create(OWNER, CONTRIBUTOR, reason="initial workspace")
        second = workspaces.create(OWNER, CONTRIBUTOR, reason="repeat request")
        assert first["workspace_key"] == second["workspace_key"]
        assert "/" not in first["workspace_key"] and ".." not in first["workspace_key"]
        assert first["path"].parent == Path(tmpdir).resolve()
        assert first["path"].suffix == ".sqlite3"


def test_nonowner_cannot_create_workspace_and_disabled_workspace_is_not_deleted():
    with tempfile.TemporaryDirectory() as tmpdir:
        _store, _roles, workspaces = _setup(tmpdir)
        created = workspaces.create(OWNER, CONTRIBUTOR, reason="initial workspace")
        try:
            workspaces.create(READER, CONTRIBUTOR, reason="not authorized")
        except PermissionError:
            pass
        else:
            raise AssertionError("non-owner created a contributor workspace")
        result = workspaces.disable(OWNER, CONTRIBUTOR, reason="access paused")
        assert result["enabled"] is False
        assert created["path"] == result["path"]
        assert result["path"].parent.exists()


def test_revoked_contributor_cannot_reopen_an_enabled_workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        _store, roles, workspaces = _setup(tmpdir)
        workspaces.create(OWNER, CONTRIBUTOR, reason="initial workspace")
        roles.revoke(OWNER, CONTRIBUTOR, reason="access revoked")
        try:
            workspaces.for_contributor(CONTRIBUTOR)
        except PermissionError:
            pass
        else:
            raise AssertionError("revoked contributor reopened an enabled workspace")


def test_owner_can_disable_auto_sync_without_disabling_workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        _store, _roles, workspaces = _setup(tmpdir)
        workspaces.create(OWNER, CONTRIBUTOR, reason="initial workspace")
        changed = workspaces.set_auto_sync(OWNER, CONTRIBUTOR, enabled=False, reason="canary paused")
        assert changed["enabled"] is True
        assert changed["auto_sync_additions"] is False


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"{len(tests)} passed — contributor workspace registry is owner-gated")
