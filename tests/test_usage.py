#!/usr/bin/env python3
"""Per-user usage metering and budget-driven rung rotation. Temp DB, fakes only, no network."""
import os
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import llm                                          # noqa: E402
from kash.store import Store                                  # noqa: E402
from kash.usage import (SYSTEM, Usage, budget_for,            # noqa: E402
                        format_usage, within_budget)

CFG = {
    "backend": "auto",
    "ladders": {"chat": ["codex", "claude_cli", "agy_cli"]},
    "budgets": {"default": {"requests_per_day": 3},
                "system": {"requests_per_day": 50},
                "per_actor": {"999": {"requests_per_day": 10}}},
}


def fresh():
    s = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    return s, Usage(s)


# --- recording ------------------------------------------------------------------------------

def test_a_call_is_recorded_as_one_request():
    _, u = fresh()
    u.record("alice", "codex")
    assert u.spent_today("alice")["requests"] == 1


def test_real_tokens_are_stored_and_flagged_as_measured():
    s, u = fresh()
    u.record("alice", "claude_cli", usage={"input_tokens": 10, "output_tokens": 400})
    got = u.spent_today("alice")
    assert got["input_tokens"] == 10 and got["output_tokens"] == 400
    assert s.conn.execute("SELECT estimated FROM llm_usage").fetchone()[0] == 0


def test_a_backend_without_token_counts_is_flagged_estimated():
    """codex and agy_cli report nothing — a guess must never look like a measurement."""
    s, u = fresh()
    u.record("alice", "codex")
    assert s.conn.execute("SELECT estimated FROM llm_usage").fetchone()[0] == 1


def test_usage_is_tracked_per_backend():
    _, u = fresh()
    u.record("alice", "codex")
    u.record("alice", "codex")
    u.record("alice", "claude_cli")
    assert u.by_backend_today("alice") == {"codex": 2, "claude_cli": 1}


def test_actors_are_isolated():
    _, u = fresh()
    u.record("alice", "codex")
    assert u.spent_today("bob")["requests"] == 0


def test_yesterdays_usage_does_not_count_today():
    s, u = fresh()
    y = (date.today() - timedelta(days=1)).isoformat()
    s.conn.execute("INSERT INTO llm_usage (ts,day,actor,backend,job,requests) "
                   "VALUES (?,?,?,?,?,1)", (y, y, "alice", "codex", "chat"))
    s.conn.commit()
    assert u.spent_today("alice")["requests"] == 0


def test_recording_never_raises():
    """Bookkeeping must not be able to fail a user's actual query."""
    s, u = fresh()
    s.conn.close()                      # make any write fail
    u.record("alice", "codex")          # must swallow it


# --- budgets --------------------------------------------------------------------------------

def test_per_actor_budget_wins_over_default():
    assert budget_for(CFG, "999")["requests_per_day"] == 10
    assert budget_for(CFG, "alice")["requests_per_day"] == 3


def test_system_has_its_own_bucket():
    assert budget_for(CFG, SYSTEM)["requests_per_day"] == 50


def test_within_budget_is_per_rung_not_global():
    """The point of rotation: spending codex must not lock you out of claude_cli."""
    _, u = fresh()
    for _ in range(3):
        u.record("alice", "codex")
    assert within_budget(CFG, u, "alice", "codex") is False
    assert within_budget(CFG, u, "alice", "claude_cli") is True


def test_no_configured_limit_means_unlimited():
    _, u = fresh()
    for _ in range(99):
        u.record("alice", "codex")
    assert within_budget({"budgets": {}}, u, "alice", "codex") is True


# --- rotation -------------------------------------------------------------------------------

def test_under_budget_keeps_the_configured_order():
    s, _ = fresh()
    assert llm.route(CFG, job="chat", actor="alice", store=s).names == [
        "codex", "claude_cli", "agy_cli"]


def test_exhausting_a_rung_demotes_it():
    s, u = fresh()
    for _ in range(3):
        u.record("alice", "codex")
    assert llm.route(CFG, job="chat", actor="alice", store=s).names == [
        "claude_cli", "agy_cli", "codex"]


def test_exhausting_two_rungs_promotes_the_third():
    s, u = fresh()
    for _ in range(3):
        u.record("alice", "codex")
        u.record("alice", "claude_cli")
    assert llm.route(CFG, job="chat", actor="alice", store=s).names[0] == "agy_cli"


def test_all_rungs_spent_falls_back_rather_than_refusing():
    s, u = fresh()
    for b in ("codex", "claude_cli", "agy_cli"):
        for _ in range(3):
            u.record("alice", b)
    assert llm.route(CFG, job="chat", actor="alice", store=s).names == [
        "codex", "claude_cli", "agy_cli"], "must keep answering, not go silent"


def test_a_pin_overrides_rotation():
    s, u = fresh()
    for _ in range(9):
        u.record("alice", "codex")
    assert llm.route(CFG, "codex", job="chat", actor="alice", store=s).names == ["codex"]


def test_one_actor_exhausting_does_not_move_another():
    s, u = fresh()
    for _ in range(3):
        u.record("alice", "codex")
    assert llm.route(CFG, job="chat", actor="bob", store=s).names[0] == "codex"


def test_routing_without_an_actor_is_unchanged():
    assert llm.route(CFG, job="chat").names == ["codex", "claude_cli", "agy_cli"]


def test_accounting_failure_does_not_break_routing():
    s, _ = fresh()
    s.conn.close()
    assert llm.route(CFG, job="chat", actor="alice", store=s).names == [
        "codex", "claude_cli", "agy_cli"]


# --- report ---------------------------------------------------------------------------------

def test_usage_report_shows_remaining_per_rung():
    _, u = fresh()
    u.record("alice", "codex")
    text = format_usage(CFG, u, "alice")
    assert "codex: 1" in text and "2 left" in text


def test_usage_report_when_nothing_spent():
    _, u = fresh()
    assert "No model calls yet today" in format_usage(CFG, u, "alice")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
