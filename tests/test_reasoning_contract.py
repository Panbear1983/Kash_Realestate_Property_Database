#!/usr/bin/env python3
"""reasoning_contract.prompt()/repair_prompt(): pure string assembly, no I/O. The module
docstring requires it stay that way — these tests only ever pass plain strings in."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import reasoning_contract as rc  # noqa: E402


def test_prompt_includes_vocabulary_block_when_given():
    text = rc.prompt("cheap homes", ["list_price"], vocabulary="property_type: sf_detached")
    assert "property_type: sf_detached" in text


def test_prompt_omits_vocabulary_block_when_empty():
    text = rc.prompt("cheap homes", ["list_price"])
    assert "property_type" not in text


def test_prompt_includes_optional_context_with_ignore_if_unrelated_wording():
    text = rc.prompt("under 700k", ["list_price"], context="status = active")
    assert "status = active" in text
    assert "OPTIONAL" in text
    assert "ignore this context completely" in text


def test_prompt_omits_context_block_when_empty():
    text = rc.prompt("under 700k", ["list_price"])
    assert "OPTIONAL background" not in text


def test_prompt_with_no_extras_is_unchanged_from_the_original_two_arg_call():
    """No signature break for the one pre-existing caller (chat.py::_prompt)."""
    text = rc.prompt("hello", ["tier", "list_price"])
    assert "Database columns (names only): tier, list_price" in text
    assert "User: hello" in text


def test_repair_prompt_names_required_keys_and_never_echoes_reply_content():
    original = rc.prompt("hello", ["tier"])
    sentinel = "SECRET_ROW_CONTENT_MUST_NOT_APPEAR"
    out = rc.repair_prompt(original, {"junk": sentinel})
    assert sentinel not in out
    assert "dict" in out            # only the TYPE of the invalid reply is named
    for key in rc.ROUTE_SCHEMA["required"]:
        assert key in out


def test_repair_prompt_appends_to_not_replaces_the_original_prompt():
    original = rc.prompt("a specific question", ["tier"], vocabulary="tier: A, B, C")
    out = rc.repair_prompt(original, None)
    assert out.startswith(original)
    assert "a specific question" in out
    assert "tier: A, B, C" in out


def test_repair_prompt_names_the_route_names_too():
    out = rc.repair_prompt(rc.prompt("x", []), "not a dict")
    for route in rc.ROUTES:
        assert route in out


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
