#!/usr/bin/env python3
"""Privacy and isolation contracts for short-lived Kash private sessions."""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kash.chat_sessions import SessionStore  # noqa: E402

PRIVATE_TEXT = "PRIVATE_PROMPT 123 MAIN STREET"


def _store():
    directory = tempfile.TemporaryDirectory()
    return directory, SessionStore(os.path.join(directory.name, "sessions.sqlite"), ttl_seconds=1800)


def test_private_sessions_are_isolated_and_store_structured_state_only():
    directory, sessions = _store()
    try:
        sessions.save(101, filters={"status": "active", "max_price": 750000},
                      listing_keys=["a", "b"], selected_key="a", raw_text=PRIVATE_TEXT, now=100)
        sessions.save(202, filters={"status": "active", "neighborhood": "Great Kills"},
                      listing_keys=["c"], now=100)
        first = sessions.load(101, now=101)
        second = sessions.load(202, now=101)
        assert first["listing_keys"] == ("a", "b") and first["selected_key"] == "a"
        assert second["listing_keys"] == ("c",) and second["selected_key"] is None
        assert first["filters"] == {"status": "active", "max_price": 750000}
        assert PRIVATE_TEXT.encode() not in open(sessions.path, "rb").read()
    finally:
        directory.cleanup()


def test_search_result_state_keeps_only_match_keys_not_listing_rows():
    directory, sessions = _store()
    try:
        rows = [{"match_key": "safe-a", "street_address": "PRIVATE 123 MAIN"},
                {"match_key": "safe-b", "my_notes": "PRIVATE NOTE"}]
        sessions.save_search(101, filters={"status": "active"}, rows=rows, now=100)
        state = sessions.load(101, now=101)
        assert state["listing_keys"] == ("safe-a", "safe-b")
        blob = open(sessions.path, "rb").read()
        assert b"PRIVATE 123 MAIN" not in blob and b"PRIVATE NOTE" not in blob
    finally:
        directory.cleanup()


def test_expired_or_cleared_session_is_unavailable_to_its_owner_only():
    directory, sessions = _store()
    try:
        sessions.save(101, filters={"status": "active"}, listing_keys=["a"], now=100)
        sessions.save(202, filters={"status": "active"}, listing_keys=["b"], now=100)
        assert sessions.load(101, now=1901) is None
        assert sessions.load(202, now=101)["listing_keys"] == ("b",)
        assert sessions.clear(202)
        assert sessions.load(202, now=101) is None
    finally:
        directory.cleanup()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — private session state is isolated and expiring")
