#!/usr/bin/env python3
"""Focused offline tests for Robo Kash reasoning-router Phases 1-2."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import chat, reasoning_router                         # noqa: E402


class BombStore:
    """Any database/access/listing interaction fails the test immediately."""

    def __getattribute__(self, name):
        raise AssertionError(f"database access attempted: {name}")


class BombBackend:
    """Any model interaction fails the test immediately."""

    def __getattribute__(self, name):
        raise AssertionError(f"model access attempted: {name}")


PREFS = {"chat": {"enabled": True}}


def _answer(text):
    answer = reasoning_router.try_answer(text)
    assert answer is not None, f"not routed as math: {text!r}"
    return answer


def test_exact_arithmetic():
    assert _answer("2 + 3 * 4").text == "14"
    assert _answer("(2 + 3) * 4").text == "20"
    assert _answer("0.1 + 0.2").text == "0.3"
    assert _answer("1 / 3 + 1 / 6").text == "0.5"
    assert _answer("1 / 3").text == "1/3"
    assert _answer("-7 / 2").text == "-3.5"


def test_one_variable_linear_equations():
    assert _answer("solve 2x + 3 = 11").text == "x = 4"
    assert _answer("3 * (x - 2) = x + 4").text == "x = 5"
    assert _answer("x / 3 + 1 = 2").text == "x = 3"
    assert _answer("2y + 1 = 9").text == "y = 4"
    assert _answer("a + 7 = 10").text == "a = 3"
    assert _answer("2x + 1 = 2x + 1").text == "Every value of x is a solution."
    assert _answer("2x + 1 = 2x + 2").text == "That equation has no solution."


def test_non_math_messages_do_not_claim_a_tool():
    assert reasoning_router.route("show homes under 700k") is None
    assert reasoning_router.route("500-700k homes") is None
    assert reasoning_router.route("help") is None
    assert reasoning_router.route("which listings have low flood risk?") is None


def test_dispatch_is_allowlisted():
    call = reasoning_router.ToolCall("shell", "2 + 2")
    answer = reasoning_router.execute(call)
    assert answer.ok is False
    assert answer.text == reasoning_router.SAFE_REPLY


def test_unsafe_malformed_and_unsupported_math_fail_closed():
    rejected = (
        "calculate __import__('os').system('id')",
        "calculate 1; DROP TABLE listings",
        "calculate 2 ** 100",
        "calculate 1 / 0",
        "calculate (2 + 3",
        "calculate ",
        "2 + bananas",
        "solve x * x = 4",
        "solve x + y = 2",
        "solve x + 1 = 2 = 3",
    )
    for text in rejected:
        answer = _answer(text)
        assert answer.ok is False, text
        assert answer.text == reasoning_router.SAFE_REPLY, text


def test_chat_math_short_circuits_model_and_all_database_access():
    for text, expected in (
        ("what is 12 / 3?", "4"),
        ("solve 5x - 10 = 0", "x = 2"),
        ("calculate __import__('os')", reasoning_router.SAFE_REPLY),
        ("sqrt(4)", reasoning_router.SAFE_REPLY),
    ):
        reply = chat.handle(999, "No DB", text, store=BombStore(), prefs=PREFS,
                            backend=BombBackend())
        assert reply.kind == "math"
        assert reply.text == expected
        assert reply.backend is None


def test_existing_non_math_route_is_unchanged():
    """Non-math still proceeds to the existing access/database boundary."""
    try:
        chat.handle(999, "No DB", "help", store=BombStore(), prefs=PREFS,
                    backend=BombBackend())
        raise AssertionError("non-math unexpectedly bypassed the existing boundary")
    except AssertionError as error:
        assert "database access attempted" in str(error)


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"{len(tests)} passed — pure allowlisted math router")
