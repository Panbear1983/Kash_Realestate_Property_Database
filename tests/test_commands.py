#!/usr/bin/env python3
"""Deterministic Telegram commands. No LLM, no network, in-memory DB only."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import commands                                   # noqa: E402
from kash.access import Access                              # noqa: E402
from kash.readonly import ReadOnlyStore                     # noqa: E402
from kash.store import Store                                # noqa: E402
from kash.usage import Usage                                # noqa: E402

PREFS = {"llm": {"budgets": {"default": {"requests_per_day": 40}}}}


def _ctx(level="read", user_id=101, rows=3):
    store = Store(":memory:")
    for i in range(rows):
        store.upsert({"street_address": f"{i} Fairlawn Loop", "zip": "10308",
                      "list_price": 700000 + i * 1000, "status": "active", "tier": "A",
                      "beds": 3, "baths": "2.5", "listing_url": f"https://ex.com/{i}",
                      "my_notes": "SELLER IS DESPERATE", "analysis": "PRIVATE THESIS"},
                     "fixture")
    access = Access(store)
    access.add(user_id, "Tester")
    return commands.Context(user_id=user_id, access_level=level, store=store,
                            ro=ReadOnlyStore(store), prefs=PREFS, access=access)


# --- routing ----------------------------------------------------------------------------------

def test_questions_fall_through_to_the_model():
    ctx = _ctx()
    for question in ("cheapest homes under 700k", "what's the flood risk in Great Kills?",
                     "hello", "help me find a 3 bed", "show me something nice today please"):
        assert commands.try_handle(question, ctx) is None, question


def test_bare_help_is_a_command_but_help_me_is_a_question():
    ctx = _ctx()
    assert commands.try_handle("help", ctx) is not None
    assert commands.try_handle("  HELP  ", ctx) is not None
    assert commands.try_handle("help me find a home", ctx) is None


def test_slash_commands_work_and_tolerate_the_group_suffix():
    ctx = _ctx()
    assert commands.try_handle("/help", ctx) is not None
    assert commands.try_handle("/help@KashBot", ctx) is not None
    assert commands.try_handle("/start", ctx) is not None


def test_empty_input_falls_through():
    ctx = _ctx()
    assert commands.try_handle("", ctx) is None
    assert commands.try_handle("   ", ctx) is None
    assert commands.try_handle("/", ctx) is None


# --- help / greeting ----------------------------------------------------------------------------

def test_help_lists_the_commands():
    text = commands.try_handle("help", _ctx())
    for expected in ("filter", "show", "usage", "model"):
        assert expected in text


def test_custom_greeting_is_prepended_when_set():
    ctx = _ctx()
    ctx.access.edit(ctx.user_id, custom_greeting="Welcome back, Jane!")
    assert commands.try_handle("/start", ctx).startswith("Welcome back, Jane!")


# --- usage --------------------------------------------------------------------------------------

def test_usage_reports_todays_calls():
    ctx = _ctx()
    Usage(ctx.store).record(ctx.user_id, "codex")
    text = commands.try_handle("usage", ctx)
    assert "codex: 1" in text


def test_usage_with_nothing_spent():
    assert "No model calls yet today" in commands.try_handle("usage", _ctx())


# --- model pinning --------------------------------------------------------------------------------

def test_model_with_no_argument_shows_current_and_choices():
    text = commands.try_handle("model", _ctx())
    assert "auto" in text and "codex" in text


def test_model_pins_and_persists():
    ctx = _ctx()
    assert "claude_cli" in commands.try_handle("model claude_cli", ctx)
    assert ctx.access.get_model_override(ctx.user_id) == "claude_cli"


def test_model_auto_clears_the_pin():
    ctx = _ctx()
    commands.try_handle("model codex", ctx)
    commands.try_handle("model auto", ctx)
    assert ctx.access.get_model_override(ctx.user_id) is None


def test_unknown_model_is_rejected_via_slash_form():
    ctx = _ctx()
    assert "don't know" in commands.try_handle("/model gpt-9", ctx)
    assert ctx.access.get_model_override(ctx.user_id) is None


def test_bare_model_followed_by_prose_is_a_question_not_a_command():
    """'model' is a normal English word; only a recognised backend name makes it a command."""
    assert commands.try_handle("model homes in great kills", _ctx()) is None


# --- filter --------------------------------------------------------------------------------------

def test_filter_runs_and_renders():
    text = commands.try_handle("filter status=active sort:list_price limit:5", _ctx())
    assert "Fairlawn Loop" in text
    assert "$700,000" in text


def test_compact_legacy_filter_uses_the_same_policy_gated_command():
    text = commands.try_handle("tier=A list_price<=750000", _ctx())
    assert "Fairlawn Loop" in text
    assert "$700,000" in text


def test_filter_on_a_private_column_is_refused():
    text = commands.try_handle("filter my_notes~DESPERATE", _ctx())
    assert "can't filter on that" in text
    assert "DESPERATE" not in text


def test_owner_may_filter_a_private_column():
    text = commands.try_handle("filter my_notes~DESPERATE", _ctx(level="owner"))
    assert "Fairlawn Loop" in text


def test_filter_with_an_unbalanced_quote_does_not_raise():
    assert "couldn't parse" in commands.try_handle('filter neighborhood="Great Kills', _ctx())


def test_filter_limit_is_clamped_by_policy():
    """`limit:-1` is unlimited in SQLite; the shared sanitizer has to catch typed input too."""
    ctx = _ctx(rows=30)
    text = commands.try_handle("filter status=active limit:-1", ctx)
    assert text.count("🔗") == 1


def test_bare_filter_shows_the_syntax():
    assert "tier=A" in commands.try_handle("filter", _ctx())


# --- show -----------------------------------------------------------------------------------------

def test_show_finds_by_partial_address():
    text = commands.try_handle("show 1 Fairlawn", _ctx())
    assert "Fairlawn Loop" in text


def test_show_reports_a_miss():
    assert "No listing matching" in commands.try_handle("show 999 Nowhere Street", _ctx())


def test_bare_show_without_a_house_number_is_a_question():
    """'show me something nice' is prose; only an address-shaped argument is a lookup."""
    ctx = _ctx()
    assert commands.try_handle("show me something nice", ctx) is None
    assert commands.try_handle("show all listings", ctx) is None
    assert commands.try_handle("/show me something nice", ctx) is not None   # slash always wins


# --- redaction -----------------------------------------------------------------------------------

def test_rendered_rows_hide_private_fields_from_a_read_user():
    text = commands.try_handle("filter status=active", _ctx())
    assert "PRIVATE THESIS" not in text
    assert "DESPERATE" not in text


def test_rendered_rows_show_analysis_to_the_owner():
    text = commands.try_handle("filter status=active", _ctx(level="owner"))
    assert "PRIVATE THESIS" in text


# --- read-only ---------------------------------------------------------------------------------------

def test_commands_never_write_to_listings():
    ctx = _ctx()
    before = ctx.store.all()
    for text in ("filter status=active", "show Fairlawn", "help", "usage", "model auto"):
        commands.try_handle(text, ctx)
    assert ctx.store.all() == before


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — deterministic commands answer without a model")
