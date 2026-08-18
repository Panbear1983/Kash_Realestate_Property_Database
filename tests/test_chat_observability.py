#!/usr/bin/env python3
"""Offline privacy regressions for Kash's aggregate-only quality ledger."""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.chat_observability import QualityLedger  # noqa: E402


PRIVATE_MESSAGE = "PRIVATE_USER_QUESTION_123 MAIN STREET"
PRIVATE_REPLY = "PRIVATE_LISTING_REPLY_456 MAIN STREET"


def _ledger():
    directory = tempfile.TemporaryDirectory()
    return directory, QualityLedger(os.path.join(directory.name, "quality.sqlite"))


def test_reply_event_contains_only_allowlisted_metadata():
    directory, ledger = _ledger()
    try:
        ledger.record_reply(kind="database_aggregate", surface="telegram", elapsed_seconds=1.4,
                            release="test-v1", message=PRIVATE_MESSAGE, reply=PRIVATE_REPLY,
                            user_id=7512954760)
        row = sqlite3.connect(ledger.path).execute(
            "SELECT surface, route, outcome, latency_bucket, release FROM quality_events"
        ).fetchone()
        assert row == ("telegram", "database_aggregate", "success", "1_to_3s", "test-v1")
        blob = open(ledger.path, "rb").read()
        assert PRIVATE_MESSAGE.encode() not in blob
        assert PRIVATE_REPLY.encode() not in blob
        assert b"7512954760" not in blob
    finally:
        directory.cleanup()


def test_feedback_is_an_enum_not_free_text_or_identity():
    directory, ledger = _ledger()
    try:
        assert ledger.record_feedback("wrong", surface="telegram", message=PRIVATE_MESSAGE,
                                      user_id=7512954760)
        assert not ledger.record_feedback("write a free-form complaint", surface="telegram")
        rows = sqlite3.connect(ledger.path).execute(
            "SELECT surface, route, outcome FROM quality_events"
        ).fetchall()
        assert rows == [("telegram", "feedback", "wrong")]
        blob = open(ledger.path, "rb").read()
        assert PRIVATE_MESSAGE.encode() not in blob
        assert b"7512954760" not in blob
    finally:
        directory.cleanup()


def test_threshold_report_is_aggregate_only_and_silent_when_healthy():
    directory, ledger = _ledger()
    try:
        assert ledger.report() == ""
        for _ in range(3):
            ledger.record_feedback("wrong", surface="telegram")
        report = ledger.report()
        assert "Kash quality report" in report
        assert "feedback wrong: 3" in report
        assert PRIVATE_MESSAGE not in report
    finally:
        directory.cleanup()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — quality telemetry is aggregate-only and redacted")
