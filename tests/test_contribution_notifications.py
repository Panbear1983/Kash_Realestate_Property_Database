#!/usr/bin/env python3
"""Both-ways contribution pings: owner on submit, submitter on decision. All offline;
the push is a recorder (or a bomb) — a push problem must never break the workflow."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.contribution_admin import ContributionAdmin            # noqa: E402
from kash.contribution_notifications import ProposalNotifier     # noqa: E402
from kash.contributions import ContributionService               # noqa: E402
from kash.data_roles import DataRoles, ROLE_CONTRIBUTOR, ROLE_REVIEWER  # noqa: E402
from kash.review_queue import ReviewQueue                        # noqa: E402
from kash.store import Store                                     # noqa: E402

OWNER, CONTRIBUTOR, REVIEWER = 1, 2, 3
VALID = {"street_address": "10 Example Road", "zip": "10301", "list_price": 700000,
         "status": "active"}


class FakePush:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def __call__(self, text, chat_ids):
        if self.fail:
            raise RuntimeError("network down")
        self.sent.append((tuple(chat_ids), text))
        return {int(uid): "sent" for uid in chat_ids}


def _setup(push=None):
    store = Store(":memory:")
    roles = DataRoles(store)
    roles.bootstrap_owner(OWNER)
    roles.grant(OWNER, CONTRIBUTOR, ROLE_CONTRIBUTOR, reason="test")
    roles.grant(OWNER, REVIEWER, ROLE_REVIEWER, reason="test")
    service = ContributionService(store, roles)
    push = push if push is not None else FakePush()
    notifier = ProposalNotifier(store, roles, push)
    admin = ContributionAdmin(service, ReviewQueue(store, roles), notifier=notifier)
    return store, service, admin, notifier, push


def _submit(service):
    return service.submit_add(CONTRIBUTOR, VALID,
                              source_url="https://example.test/listing/10",
                              observed_at="2026-08-25")["id"]


def test_submit_ping_reaches_every_owner_once_with_a_summary():
    store, service, _admin, notifier, push = _setup()
    pid = _submit(service)
    notifier.submitted(pid)
    notifier.submitted(pid)                       # retried submit — ledger dedupes
    assert len(push.sent) == 1
    recipients, text = push.sent[0]
    assert recipients == (OWNER,)
    assert f"#{pid}" in text and "add" in text
    assert "10 Example Road" in text and "$700,000" in text and "F3" in text
    store.close()


def test_decision_pings_reach_the_submitter_with_the_reason():
    store, service, admin, _notifier, push = _setup()
    pid = _submit(service)
    admin.approve(REVIEWER, pid, reason="verified against the county record")
    assert push.sent[-1][0] == (CONTRIBUTOR,)
    assert "approved" in push.sent[-1][1]
    assert "verified against the county record" in push.sent[-1][1]

    pid2 = _submit_second(service)
    admin.reject(REVIEWER, pid2, reason="no comparable evidence")
    assert push.sent[-1][0] == (CONTRIBUTOR,)
    assert "rejected" in push.sent[-1][1] and "no comparable evidence" in push.sent[-1][1]
    store.close()


def _submit_second(service):
    return service.submit_add(
        CONTRIBUTOR, {**VALID, "street_address": "11 Example Road"},
        source_url="https://example.test/listing/11", observed_at="2026-08-25")["id"]


def test_publish_ping_and_ledger_dedup():
    store, service, admin, notifier, push = _setup()
    pid = _submit(service)
    admin.approve(REVIEWER, pid, reason="ok")
    admin.publish(OWNER, pid)
    published = [t for _, t in push.sent if "published" in t]
    assert len(published) == 1
    notifier.decision(pid, "published")           # replay — deduped
    assert len([t for _, t in push.sent if "published" in t]) == 1
    store.close()


def test_a_raising_push_never_breaks_the_review_action():
    store, service, admin, _notifier, push = _setup(push=FakePush(fail=True))
    pid = _submit(service)
    admin.approve(REVIEWER, pid, reason="ok")     # must not raise
    admin.publish(OWNER, pid)                     # must not raise
    row = store.conn.execute("SELECT status FROM listing_proposals WHERE id=?",
                             (pid,)).fetchone()
    assert row[0] == "published"
    store.close()


def test_self_decision_and_owner_submitter_are_silent():
    store, service, admin, notifier, push = _setup()
    # the OWNER submitting: no submit ping to themself
    pid = service.submit_add(OWNER, {**VALID, "street_address": "12 Example Road"},
                             source_url="https://example.test/listing/12",
                             observed_at="2026-08-25")["id"]
    notifier.submitted(pid)
    assert push.sent == []
    # a reviewer deciding their own proposal is impossible (workflow refuses), so
    # exercise the notifier guard directly:
    notifier.decision(pid, "approved", actor_id=OWNER)
    assert push.sent == []
    store.close()


def test_admin_without_a_notifier_stays_backward_compatible():
    store = Store(":memory:")
    roles = DataRoles(store)
    roles.bootstrap_owner(OWNER)
    roles.grant(OWNER, CONTRIBUTOR, ROLE_CONTRIBUTOR, reason="test")
    roles.grant(OWNER, REVIEWER, ROLE_REVIEWER, reason="test")
    service = ContributionService(store, roles)
    admin = ContributionAdmin(service, ReviewQueue(store, roles))
    pid = _submit(service)
    admin.approve(REVIEWER, pid, reason="ok")
    admin.publish(OWNER, pid)
    store.close()


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — contribution pings are ledgered, best-effort, both ways")
