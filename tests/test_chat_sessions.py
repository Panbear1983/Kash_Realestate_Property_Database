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


def _f(field, op, value):
    return {"field": field, "op": op, "value": value}


def test_private_sessions_are_isolated_and_store_structured_state_only():
    """filters is the real SafeSpec shape — a list of {field,op,value} dicts, the same
    shape kash.chat_policy.sanitize_spec produces — not an arbitrary flat dict. That old
    shape was never populated by any production caller (dead code)."""
    directory, sessions = _store()
    try:
        sessions.save(101, filters=[_f("status", "=", "active"), _f("list_price", "<=", "750000")],
                      listing_keys=["a", "b"], selected_key="a", raw_text=PRIVATE_TEXT, now=100)
        sessions.save(202, filters=[_f("status", "=", "active"), _f("neighborhood", "=", "Great Kills")],
                      listing_keys=["c"], now=100)
        first = sessions.load(101, now=101)
        second = sessions.load(202, now=101)
        assert first["listing_keys"] == ("a", "b") and first["selected_key"] == "a"
        assert second["listing_keys"] == ("c",) and second["selected_key"] is None
        assert first["filters"] == [_f("status", "=", "active"), _f("list_price", "<=", "750000")]
        assert PRIVATE_TEXT.encode() not in open(sessions.path, "rb").read()
    finally:
        directory.cleanup()


def test_search_result_state_keeps_only_match_keys_not_listing_rows():
    directory, sessions = _store()
    try:
        rows = [{"match_key": "safe-a", "street_address": "PRIVATE 123 MAIN"},
                {"match_key": "safe-b", "my_notes": "PRIVATE NOTE"}]
        sessions.save_search(101, filters=[_f("status", "=", "active")], rows=rows, now=100)
        state = sessions.load(101, now=101)
        assert state["listing_keys"] == ("safe-a", "safe-b")
        blob = open(sessions.path, "rb").read()
        assert b"PRIVATE 123 MAIN" not in blob and b"PRIVATE NOTE" not in blob
    finally:
        directory.cleanup()


def test_expired_or_cleared_session_is_unavailable_to_its_owner_only():
    directory, sessions = _store()
    try:
        sessions.save(101, filters=[_f("status", "=", "active")], listing_keys=["a"], now=100)
        sessions.save(202, filters=[_f("status", "=", "active")], listing_keys=["b"], now=100)
        assert sessions.load(101, now=1901) is None
        assert sessions.load(202, now=101)["listing_keys"] == ("b",)
        assert sessions.clear(202)
        assert sessions.load(202, now=101) is None
    finally:
        directory.cleanup()


def test_private_or_owner_only_filters_are_never_persisted():
    """A filter on a PRIVATE_FIELDS column must be dropped at the storage boundary — the
    routing prompt is always built at 'read' level regardless of who is actually asking,
    so a private field name must never round-trip back into a later prompt."""
    directory, sessions = _store()
    try:
        sessions.save(101, filters=[_f("status", "=", "active"), _f("my_notes", "contains", "x"),
                                    _f("analysis", "contains", "y")],
                      listing_keys=["a"], now=100)
        state = sessions.load(101, now=101)
        assert state["filters"] == [_f("status", "=", "active")]
    finally:
        directory.cleanup()


def test_stored_filter_count_and_shape_are_bounded():
    directory, sessions = _store()
    try:
        many = [_f("status", "=", "active")] * 10
        sessions.save(101, filters=many, listing_keys=["a"], now=100)
        assert len(sessions.load(101, now=101)["filters"]) == 8

        junk = [_f("status", "=", "active"), "not-a-dict", {"field": "status"},
                {"field": "status", "op": "=", "value": ["list", "value"]}]
        sessions.save(202, filters=junk, listing_keys=["b"], now=100)
        assert sessions.load(202, now=101)["filters"] == [_f("status", "=", "active")]
    finally:
        directory.cleanup()


def test_pending_is_one_shot_and_expires_on_its_own_shorter_ttl():
    directory, sessions = _store()
    with directory:
        sessions.save_pending(1, filters=[_f("beds", "=", "3")], question="Budget?", now=0)
        assert sessions.consume_pending(1, now=301) is None      # expired (300s TTL)
        sessions.save_pending(1, filters=[_f("beds", "=", "3")], question="Budget?", now=0)
        pending = sessions.consume_pending(1, now=299)
        assert pending["filters"] == [_f("beds", "=", "3")]
        assert pending["question"] == "Budget?"
        assert sessions.consume_pending(1, now=299) is None      # one-shot


def test_pending_with_only_private_filters_is_never_saved():
    directory, sessions = _store()
    with directory:
        sessions.save_pending(1, filters=[_f("my_notes", "contains", "divorce")],
                              question="Q?", now=0)
        assert sessions.consume_pending(1, now=1) is None


def test_a_doctored_pending_payload_is_dropped_on_load():
    import json
    import sqlite3
    directory, sessions = _store()
    with directory:
        sessions.save_pending(1, filters=[_f("beds", "=", "3")], question="Q?", now=0)
        con = sqlite3.connect(sessions.path)
        for evil in ("not json at all",
                     json.dumps({"filters": [{"field": "my_notes", "op": "contains",
                                              "value": "x"}],
                                 "sort": "rank", "order": "asc", "limit": 20,
                                 "question": "Q?"}),
                     json.dumps({"filters": "junk", "sort": 1, "order": [], "limit": "x",
                                 "question": None})):
            con.execute("UPDATE private_sessions SET pending_json=?, pending_expires_at=?"
                        " WHERE user_id=1", (evil, 10_000))
            con.commit()
            assert sessions.consume_pending(1, now=0) is None, evil
        con.close()


def test_pending_survives_and_migrates_a_pre_pending_schema_file():
    import sqlite3
    directory = tempfile.TemporaryDirectory()
    with directory:
        path = os.path.join(directory.name, "sessions.sqlite")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE private_sessions (user_id INTEGER PRIMARY KEY,"
                    " filters_json TEXT NOT NULL, listing_keys_json TEXT NOT NULL,"
                    " selected_key TEXT, expires_at INTEGER NOT NULL)")
        con.execute("INSERT INTO private_sessions VALUES (1, '[]', '[\"k\"]', NULL,"
                    " 9999999999)")
        con.commit()
        con.close()
        sessions = SessionStore(path)
        assert sessions.load(1)["listing_keys"] == ("k",)        # old row still readable
        sessions.save_pending(1, filters=[_f("beds", "=", "3")], question="Q?", now=0)
        assert sessions.consume_pending(1, now=1)["filters"] == [_f("beds", "=", "3")]
        assert sessions.load(1)["listing_keys"] == ("k",)        # untouched by the pending


def test_save_pending_extends_a_nearly_expired_session_row():
    directory, sessions = _store()
    with directory:
        sessions.save(1, filters=[], listing_keys=["k"], now=0)   # expires at 1800
        sessions.save_pending(1, filters=[_f("beds", "=", "3")], question="Q?", now=1799)
        # At t=1900 the original session TTL has lapsed but the pending (1799+300) has not.
        assert sessions.consume_pending(1, now=1900) is not None


def test_or_group_filters_round_trip_and_malformed_groups_drop():
    directory, sessions = _store()
    with directory:
        good = {"field": "neighborhood", "op": "=", "value": "",
                "values": ["Great Kills", "Annadale"]}
        bad_op = {"field": "list_price", "op": "<", "value": "", "values": ["1", "2"]}
        bad_vals = {"field": "neighborhood", "op": "=", "value": "", "values": [["nested"]]}
        private = {"field": "my_notes", "op": "contains", "value": "", "values": ["x"]}
        sessions.save(1, filters=[good, bad_op, bad_vals, private], listing_keys=["k"])
        loaded = sessions.load(1)
        assert loaded["filters"] == [good]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — private session state is isolated and expiring")
