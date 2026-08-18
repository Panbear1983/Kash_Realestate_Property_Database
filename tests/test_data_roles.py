#!/usr/bin/env python3
"""Role separation for supervised human listing contributions."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.data_roles import DataRoles, ROLE_CONTRIBUTOR, ROLE_OWNER  # noqa: E402
from kash.store import Store  # noqa: E402

OWNER, CONTRIBUTOR, READER = 1, 2, 3


def test_allowed_chat_access_does_not_grant_any_data_role():
    store = Store(":memory:")
    roles = DataRoles(store)
    assert roles.role_for(READER) is None
    assert not roles.can_submit(READER)


def test_only_owner_can_grant_a_narrow_contributor_role_and_audit_it():
    store = Store(":memory:")
    roles = DataRoles(store)
    roles.bootstrap_owner(OWNER)
    assert roles.role_for(OWNER) == ROLE_OWNER
    roles.grant(OWNER, CONTRIBUTOR, ROLE_CONTRIBUTOR, reason="owner approved")
    assert roles.role_for(CONTRIBUTOR) == ROLE_CONTRIBUTOR
    assert roles.can_submit(CONTRIBUTOR)
    audit = roles.audit_for(CONTRIBUTOR)
    assert audit[-1]["event"] == "grant" and audit[-1]["granted_by"] == OWNER


def test_contributor_cannot_grant_or_escalate_roles():
    store = Store(":memory:")
    roles = DataRoles(store)
    roles.bootstrap_owner(OWNER)
    roles.grant(OWNER, CONTRIBUTOR, ROLE_CONTRIBUTOR, reason="owner approved")
    try:
        roles.grant(CONTRIBUTOR, READER, ROLE_CONTRIBUTOR, reason="nope")
    except PermissionError:
        pass
    else:
        raise AssertionError("contributor escalated a role")
    assert roles.role_for(READER) is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — data roles are narrow and auditable")
