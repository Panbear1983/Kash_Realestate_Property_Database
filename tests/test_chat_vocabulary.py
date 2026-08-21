#!/usr/bin/env python3
"""The routing-prompt vocabulary cache: distinct enum values + live examples, TTL-gated,
never raising. No network; an in-memory Store stands in for the pool."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import chat_vocabulary  # noqa: E402
from kash.readonly import ReadOnlyStore  # noqa: E402
from kash.store import Store  # noqa: E402

PREFS = {"eligibility": {"safe_flood_zones": ["X"]}}


def _row(i, **over):
    r = {"street_address": f"{i} Test Ave", "zip": "10308", "list_price": 700000,
         "beds": "3", "baths": "2.5", "property_type": "sf_detached", "status": "active",
         "tier": "A", "view_priority": "worth", "flood_zone": "X",
         "neighborhood": "Great Kills"}
    r.update(over)
    return r


def _ro(rows):
    store = Store(":memory:")
    for i, r in enumerate(rows):
        store.upsert(r, source=f"test{i}")
    return ReadOnlyStore(store)


def setup_function(_fn):
    chat_vocabulary.reset_cache()


def test_build_reflects_distinct_values_and_caps_at_max_values_per_field():
    ro = _ro([_row(1, property_type="sf_detached"), _row(2, property_type="2fam_detached"),
             _row(3, property_type="sf_detached")])
    result = chat_vocabulary.build(ro, PREFS)
    block = result["prompt_block"]
    assert "sf_detached" in block and "2fam_detached" in block
    assert "property_type:" in block


def test_flood_zone_semantics_line_reads_prefs_not_hardcoded_x():
    ro = _ro([_row(1)])
    default = chat_vocabulary.build(ro, PREFS)["prompt_block"]
    assert "X = this buyer's configured safe" in default

    custom = chat_vocabulary.build(ro, {"eligibility": {"safe_flood_zones": ["X", "X500"]}})
    assert "X/X500 = this buyer's configured safe" in custom["prompt_block"]


def test_peek_prompt_block_is_empty_before_any_refresh():
    assert chat_vocabulary.peek_prompt_block() == ""
    assert chat_vocabulary.peek_examples() == ()


def test_maybe_refresh_is_ttl_gated():
    ro = _ro([_row(1)])
    chat_vocabulary.maybe_refresh(ro, PREFS, now=1000.0)
    first = chat_vocabulary.peek_prompt_block()
    assert first

    # A second store with DIFFERENT data, still inside the TTL window: must not re-query.
    ro2 = _ro([_row(1, property_type="Land")])
    chat_vocabulary.maybe_refresh(ro2, PREFS, now=1000.0 + chat_vocabulary.VOCAB_TTL_SECONDS - 1)
    assert chat_vocabulary.peek_prompt_block() == first

    chat_vocabulary.maybe_refresh(ro2, PREFS, now=1000.0 + chat_vocabulary.VOCAB_TTL_SECONDS + 1)
    assert "Land" in chat_vocabulary.peek_prompt_block()


def test_maybe_refresh_never_raises_on_a_broken_store():
    class Bomb:
        def aggregate_select(self, *a, **k):
            raise RuntimeError("no database here")

        def overall_price_summary(self):
            raise RuntimeError("no database here")

    chat_vocabulary.maybe_refresh(Bomb(), PREFS, now=1.0)   # must not raise
    assert chat_vocabulary.peek_prompt_block() == ""         # cache stays cold, not crashed


def test_examples_use_a_real_neighborhood_and_a_rounded_price_bucket():
    ro = _ro([_row(i, list_price=700000, neighborhood="Great Kills") for i in range(3)])
    result = chat_vocabulary.build(ro, PREFS)
    examples = result["examples"]
    assert any("Great Kills" in e for e in examples)
    assert any("$700,000" in e or "$" in e for e in examples)
    assert any("X" in e for e in examples)


def test_safe_clarify_with_examples_falls_back_to_the_plain_string_when_cache_is_cold():
    from kash.reasoning_contract import SAFE_CLARIFY
    assert chat_vocabulary.safe_clarify_with_examples() == SAFE_CLARIFY


def test_safe_clarify_with_examples_appends_live_examples_when_warm():
    from kash.reasoning_contract import SAFE_CLARIFY
    ro = _ro([_row(1)])
    chat_vocabulary.maybe_refresh(ro, PREFS, now=1.0)
    text = chat_vocabulary.safe_clarify_with_examples()
    assert text.startswith(SAFE_CLARIFY)
    assert "For example:" in text


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        setup_function(fn)
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
