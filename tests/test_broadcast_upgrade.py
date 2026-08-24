#!/usr/bin/env python3
"""The one-time upgrade broadcast: allowed users only, ledger-idempotent, retry on fail."""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

import broadcast_upgrade  # noqa: E402
from kash.access import Access  # noqa: E402
from kash.store import Store  # noqa: E402

ALLOWED_A, ALLOWED_B, PENDING, DENIED = 11, 22, 33, 44


def _store():
    store = Store(":memory:")
    access = Access(store)
    access.add(ALLOWED_A, "A")
    access.add(ALLOWED_B, "B")
    access.add(PENDING, "P", status="pending")
    access.add(DENIED, "D", status="denied")
    return store


class FakePush:
    def __init__(self, fail_for=()):
        self.sent = []            # (uid, text)
        self.fail_for = set(fail_for)

    def __call__(self, text, chat_ids):
        out = {}
        for uid in chat_ids:
            if uid in self.fail_for:
                out[int(uid)] = "error: boom"
            else:
                self.sent.append((int(uid), text))
                out[int(uid)] = "sent"
        return out


def test_dry_run_sends_nothing_and_names_only_allowed_users():
    store = _store()
    push = FakePush()
    outcomes = broadcast_upgrade.broadcast(store, apply=False, push=push)
    assert set(outcomes) == {ALLOWED_A, ALLOWED_B}
    assert push.sent == []
    assert all(o.startswith("would send") for o in outcomes.values())


def test_apply_sends_once_and_a_second_run_sends_zero():
    store = _store()
    push = FakePush()
    outcomes = broadcast_upgrade.broadcast(store, apply=True, push=push)
    assert outcomes == {ALLOWED_A: "sent", ALLOWED_B: "sent"}
    sent_uids = {uid for uid, _ in push.sent}
    assert sent_uids == {ALLOWED_A, ALLOWED_B}
    first_count = len(push.sent)

    outcomes = broadcast_upgrade.broadcast(store, apply=True, push=push)
    assert outcomes == {ALLOWED_A: "already sent", ALLOWED_B: "already sent"}
    assert len(push.sent) == first_count, "second run must send nothing"


def test_a_failed_send_is_unmarked_and_retried_on_rerun():
    store = _store()
    outcomes = broadcast_upgrade.broadcast(store, apply=True,
                                           push=FakePush(fail_for={ALLOWED_B}))
    assert outcomes[ALLOWED_A] == "sent"
    assert outcomes[ALLOWED_B].startswith("error")

    retry = FakePush()
    outcomes = broadcast_upgrade.broadcast(store, apply=True, push=retry)
    assert outcomes == {ALLOWED_A: "already sent", ALLOWED_B: "sent"}
    assert {uid for uid, _ in retry.sent} == {ALLOWED_B}


def test_denied_and_pending_users_never_receive():
    store = _store()
    push = FakePush()
    broadcast_upgrade.broadcast(store, apply=True, push=push)
    assert all(uid in (ALLOWED_A, ALLOWED_B) for uid, _ in push.sent)


def test_the_dashboards_internal_actor_zero_is_never_a_recipient():
    store = _store()
    Access(store).add(0, "Dashboard", status="allowed")
    push = FakePush()
    outcomes = broadcast_upgrade.broadcast(store, apply=True, push=push)
    assert 0 not in outcomes
    assert all(uid != 0 for uid, _ in push.sent)


def test_the_message_fits_telegram_chunking():
    from kash.notifications import split_text
    parts = split_text(broadcast_upgrade.MESSAGE)
    assert parts, "message must not be empty"
    assert all(len(p) <= 4096 for p in parts)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — the broadcast is idempotent and allowed-only")
