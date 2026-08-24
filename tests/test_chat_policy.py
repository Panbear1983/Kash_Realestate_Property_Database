#!/usr/bin/env python3
"""Chat policy and spec sanitizing. Pure functions plus one execution check — no LLM, no
network; the only database is in-memory and is used to prove a sanitized spec actually runs.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import chat_policy as cp                      # noqa: E402
from kash import query                                  # noqa: E402
from kash.schema import FIELD_ORDER                     # noqa: E402
from kash.store import Store                            # noqa: E402


def _spec(**changes):
    base = {"filters": [], "sort": "rank", "order": "asc", "limit": 20}
    base.update(changes)
    return base


def _one(field, op, value):
    return _spec(filters=[{"field": field, "op": op, "value": value}])


# --- limits ---------------------------------------------------------------------------------

def test_negative_limit_is_clamped_not_passed_through():
    """LIMIT -1 is unlimited in SQLite — this is the pool-dump guard."""
    safe = cp.sanitize_spec(_spec(limit=-1))
    assert safe.limit == cp.MIN_LIMIT
    assert any(d.startswith("limit_below_minimum") for d in safe.dropped)


def test_zero_limit_is_clamped():
    assert cp.sanitize_spec(_spec(limit=0)).limit == cp.MIN_LIMIT


def test_oversized_limit_is_capped():
    safe = cp.sanitize_spec(_spec(limit=100000))
    assert safe.limit == cp.MAX_LIMIT
    assert any(d.startswith("limit_above_maximum") for d in safe.dropped)


def test_non_numeric_limit_falls_back_instead_of_raising():
    """kash.nl does int(spec['limit']) outside its try block; this is that crash removed."""
    for bad in ("abc", None, [], {"a": 1}, float("nan")):
        safe = cp.sanitize_spec(_spec(limit=bad))
        assert cp.MIN_LIMIT <= safe.limit <= cp.MAX_LIMIT, bad


def test_float_limit_is_accepted():
    assert cp.sanitize_spec(_spec(limit=12.9)).limit == 12


# --- private columns ------------------------------------------------------------------------

def test_private_columns_cannot_be_filtered():
    for field in ("my_notes", "analysis", "target_buy_price", "offer_status", "user_rating"):
        safe = cp.sanitize_spec(_one(field, "contains", "x"))
        assert safe.filters == [], f"{field} survived sanitizing"
        assert any(d.startswith("field_not_available") for d in safe.dropped)


def test_private_columns_cannot_be_sorted_by():
    safe = cp.sanitize_spec(_spec(sort="my_notes"))
    assert safe.sort == cp.DEFAULT_SORT
    assert any(d.startswith("sort_not_available") for d in safe.dropped)


def test_the_owner_can_reach_private_columns():
    safe = cp.sanitize_spec(_one("my_notes", "contains", "motivated"), access_level="owner")
    assert safe.filters == [{"field": "my_notes", "op": "contains", "value": "motivated"}]


def test_missing_and_private_columns_are_reported_identically():
    """Confirming that my_notes exists but is restricted is itself a leak."""
    private = cp.sanitize_spec(_one("my_notes", "=", "x")).dropped
    unknown = cp.sanitize_spec(_one("no_such_column", "=", "x")).dropped
    assert private[0].split(":")[0] == unknown[0].split(":")[0] == "field_not_available"


def test_shared_columns_stay_queryable():
    """tier/view_priority/rank already go to every alert recipient; keep them usable."""
    for field in ("tier", "view_priority", "rank", "list_price", "flood_zone", "zip"):
        assert field in cp.CHAT_FIELDS, field
    assert cp.sanitize_spec(_spec(sort="list_price", order="desc")).sort == "list_price"


def test_private_and_chat_fields_partition_the_schema():
    assert cp.PRIVATE_FIELDS <= frozenset(FIELD_ORDER), "policy names a column that is gone"
    assert cp.CHAT_FIELDS | cp.PRIVATE_FIELDS == frozenset(FIELD_ORDER)
    assert not (cp.CHAT_FIELDS & cp.PRIVATE_FIELDS)


# --- crash paths ----------------------------------------------------------------------------

def test_null_value_is_dropped_rather_than_raising_typeerror():
    safe = cp.sanitize_spec(_one("list_price", "<", None))
    assert safe.filters == []
    assert any(d.startswith("null_value") for d in safe.dropped)


def test_non_numeric_value_on_a_numeric_column_is_dropped():
    safe = cp.sanitize_spec(_one("list_price", "<", "cheap"))
    assert safe.filters == []
    assert any(d.startswith("non_numeric_value") for d in safe.dropped)


def test_contains_on_a_numeric_column_is_kept_because_like_never_casts():
    safe = cp.sanitize_spec(_one("list_price", "contains", "999"))
    assert safe.filters == [{"field": "list_price", "op": "contains", "value": "999"}]


def test_non_scalar_values_are_dropped():
    for value in ([1, 2], {"a": 1}, (1,)):
        safe = cp.sanitize_spec(_one("zip", "=", value))
        assert safe.filters == [], value


def test_malformed_specs_degrade_to_a_sane_default():
    for bad in (None, "not a spec", 42, [], {"filters": "nope"}):
        safe = cp.sanitize_spec(bad)
        assert safe.filters == [] and safe.sort == cp.DEFAULT_SORT
        assert safe.limit == cp.DEFAULT_LIMIT or safe.limit == cp.MIN_LIMIT


def test_junk_filter_entries_are_skipped_individually():
    """One bad filter must not discard the good ones alongside it."""
    safe = cp.sanitize_spec(_spec(filters=[
        "not an object",
        {"field": "my_notes", "op": "contains", "value": "x"},
        {"field": "zip", "op": "=", "value": "10308"},
    ]))
    assert safe.filters == [{"field": "zip", "op": "=", "value": "10308"}]
    assert len(safe.dropped) == 2


def test_values_are_coerced_to_strings():
    safe = cp.sanitize_spec(_one("list_price", "<", 750000))
    assert safe.filters[0]["value"] == "750000"
    assert cp.sanitize_spec(_one("favorite", "=", True),
                            access_level="owner").filters[0]["value"] == "true"


# --- aliases and operators -------------------------------------------------------------------

def test_aliases_are_applied():
    assert cp.sanitize_spec(_one("price", "lte", "700000")).filters == [
        {"field": "list_price", "op": "<=", "value": "700000"}]
    assert cp.sanitize_spec(_spec(sort="bedrooms")).sort == "beds"


def test_unknown_operators_are_dropped():
    for op in ("OR 1=1", "IS", "regex", ""):
        assert cp.sanitize_spec(_one("zip", op, "10308")).filters == [], op


def test_every_alias_target_is_a_real_query_operator():
    """Drift guard: an alias pointing at an op kash.query does not implement would raise."""
    for target in set(cp.OP_ALIAS.values()):
        assert target in query._OPS, target


def test_every_field_alias_target_is_a_real_column():
    for target in set(cp.FIELD_ALIAS.values()):
        assert target in FIELD_ORDER, target


# The retired kash.nl module's private alias maps, inlined verbatim as a compatibility
# floor: every alias the old NL layer understood must keep working in chat_policy, so a
# phrasing that used to translate keeps translating identically.
_LEGACY_NL_OP_ALIAS = {"eq": "=", "equals": "=", "=": "=", "ne": "!=", "neq": "!=",
                       "!=": "!=", "lt": "<", "<": "<", "lte": "<=", "le": "<=",
                       "<=": "<=", "gt": ">", ">": ">", "gte": ">=", "ge": ">=",
                       ">=": ">=", "contains": "contains", "like": "contains",
                       "~": "contains"}
_LEGACY_NL_FIELD_ALIAS = {"price": "list_price", "bedrooms": "beds", "bathrooms": "baths",
                          "zip_code": "zip", "zipcode": "zip", "square_feet": "sqft",
                          "sqfootage": "sqft", "school_rating": "school_gs_rating"}


def test_policy_alias_maps_cover_what_the_retired_nl_layer_applied():
    for name, theirs, mine in (("_OP_ALIAS", _LEGACY_NL_OP_ALIAS, cp.OP_ALIAS),
                               ("_FIELD_ALIAS", _LEGACY_NL_FIELD_ALIAS, cp.FIELD_ALIAS)):
        missing = {k: v for k, v in theirs.items() if mine.get(k) != v}
        assert not missing, f"legacy nl {name} entries chat_policy lacks: {missing}"


def test_order_is_normalized_to_asc_or_desc():
    assert cp.sanitize_spec(_spec(order="DESC")).order == "desc"
    assert cp.sanitize_spec(_spec(order="descending")).order == "desc"
    assert cp.sanitize_spec(_spec(order="asc; DROP TABLE listings")).order == "asc"
    assert cp.sanitize_spec(_spec(order=None)).order == "asc"


# --- text clamping ---------------------------------------------------------------------------

def test_messages_and_replies_are_truncated():
    assert len(cp.clamp_message("x" * 5000)) <= cp.MAX_MESSAGE_CHARS + 1
    assert len(cp.clamp_reply("y" * 5000)) <= cp.MAX_REPLY_CHARS + 1
    assert cp.clamp_message("short question") == "short question"


def test_control_characters_are_stripped_but_newlines_survive():
    assert "\x00" not in cp.clamp_message("hi\x00there\x1b[31m")
    assert "\x1b" not in cp.clamp_reply("red\x1b[31malert")
    assert cp.clamp_reply("line one\nline two") == "line one\nline two"


def test_clamping_handles_none():
    assert cp.clamp_message(None) == "" and cp.clamp_reply(None) == ""


# --- OR groups ------------------------------------------------------------------------------

def _group(field, op, values, value=""):
    return _spec(filters=[{"field": field, "op": op, "value": value, "values": values}])


def test_or_group_is_accepted_for_equals():
    safe = cp.sanitize_spec(_group("neighborhood", "=", ["Great Kills", "Annadale"]))
    assert safe.filters == [{"field": "neighborhood", "op": "=", "value": "",
                             "values": ["Great Kills", "Annadale"]}]


def test_or_group_with_one_value_collapses_to_a_plain_filter():
    safe = cp.sanitize_spec(_group("neighborhood", "=", ["Great Kills"]))
    assert safe.filters == [{"field": "neighborhood", "op": "=", "value": "Great Kills"}]


def test_or_group_is_deduped_and_capped():
    values = ["A", "A", "B", "C", "D", "E", "F", "G"]
    safe = cp.sanitize_spec(_group("neighborhood", "=", values))
    assert safe.filters[0]["values"] == ["A", "B", "C", "D", "E"][:cp.MAX_OR_VALUES]
    assert any(d.startswith("or_group_too_large") for d in safe.dropped)


def test_or_group_with_a_comparison_operator_is_dropped():
    safe = cp.sanitize_spec(_group("list_price", "<", ["700000", "800000"]))
    assert safe.filters == []
    assert any(d.startswith("or_group_bad_operator") for d in safe.dropped)


def test_or_group_wins_when_both_value_forms_are_set():
    safe = cp.sanitize_spec(_group("neighborhood", "=", ["Great Kills", "Annadale"],
                                   value="Eltingville"))
    assert safe.filters[0]["values"] == ["Great Kills", "Annadale"]
    assert any(d.startswith("or_group_both_value_forms") for d in safe.dropped)


def test_or_group_on_a_private_field_is_dropped_with_the_undifferentiated_message():
    safe = cp.sanitize_spec(_group("my_notes", "contains", ["divorce", "estate"]))
    assert safe.filters == []
    assert any(d.startswith("field_not_available") for d in safe.dropped)


def test_or_group_that_cleans_to_nothing_is_dropped():
    safe = cp.sanitize_spec(_group("list_price", "=", ["cheap", "affordable"]))
    assert safe.filters == []
    assert any(d.startswith("or_group_empty") for d in safe.dropped)


def test_or_group_keeps_the_numeric_elements_that_survive():
    safe = cp.sanitize_spec(_group("list_price", "=", ["700000", "cheap", "800000"]))
    assert safe.filters[0]["values"] == ["700000", "800000"]
    assert any(d.startswith("non_numeric_value") for d in safe.dropped)


def test_or_group_executes_against_a_real_store():
    store = Store(":memory:")
    for hood in ("Great Kills", "Annadale", "Eltingville"):
        store.upsert({"street_address": f"1 {hood} St", "zip": "10308",
                      "neighborhood": hood, "list_price": 700000,
                      "status": "active"}, "fixture")
    safe = cp.sanitize_spec(_group("neighborhood", "=", ["Great Kills", "Annadale"]))
    rows = query.run(store, filters=safe.filters, sort=safe.sort,
                     order=safe.order, limit=safe.limit)
    assert {r["neighborhood"] for r in rows} == {"Great Kills", "Annadale"}
    store.close()


# --- aggregates -----------------------------------------------------------------------------

def test_grouped_count_aggregate_is_accepted():
    safe = cp.sanitize_spec(_spec(aggregate="count", group_by="neighborhood"))
    assert (safe.aggregate, safe.aggregate_field, safe.group_by) == ("count", "", "neighborhood")


def test_avg_requires_a_whitelisted_field():
    safe = cp.sanitize_spec(_spec(aggregate="avg"))
    assert safe.aggregate == ""
    assert any(d.startswith("aggregate_field_not_available") for d in safe.dropped)
    safe = cp.sanitize_spec(_spec(aggregate="avg", aggregate_field="my_notes"))
    assert safe.aggregate == ""
    assert any(d.startswith("aggregate_field_not_available") for d in safe.dropped)


def test_unknown_aggregate_is_cleared_with_a_reason():
    safe = cp.sanitize_spec(_spec(aggregate="median", aggregate_field="list_price"))
    assert (safe.aggregate, safe.aggregate_field, safe.group_by) == ("", "", "")
    assert any(d.startswith("aggregate_not_available") for d in safe.dropped)


def test_unknown_group_by_clears_the_whole_trio():
    safe = cp.sanitize_spec(_spec(aggregate="count", group_by="street_address"))
    assert (safe.aggregate, safe.aggregate_field, safe.group_by) == ("", "", "")
    assert any(d.startswith("group_by_not_available") for d in safe.dropped)


def test_count_ignores_a_stray_aggregate_field():
    safe = cp.sanitize_spec(_spec(aggregate="count", aggregate_field="list_price"))
    assert (safe.aggregate, safe.aggregate_field) == ("count", "")


def test_aggregate_aliases_are_applied_to_the_field():
    safe = cp.sanitize_spec(_spec(aggregate="avg", aggregate_field="price"))
    assert safe.aggregate_field == "list_price"


def test_no_aggregate_keys_means_a_plain_row_spec():
    safe = cp.sanitize_spec(_spec())
    assert (safe.aggregate, safe.aggregate_field, safe.group_by) == ("", "", "")
    assert not safe.dropped


def test_enforced_aggregate_whitelists_match_the_model_facing_contract():
    """The schema enums are hints; query.py's frozensets are the gate. Drift guard."""
    from kash import reasoning_contract as rc
    assert set(v for v in rc.AGGREGATE_OPS if v) == set(query.AGGREGATES)
    assert set(rc.AGGREGATE_FIELD_VALUES) == set(query.AGGREGATE_FIELDS)
    assert set(v for v in rc.GROUP_BY_VALUES if v) == set(query.GROUP_BY_FIELDS)
    assert rc.MAX_OR_VALUES == cp.MAX_OR_VALUES
    assert not (query.AGGREGATE_FIELDS & cp.PRIVATE_FIELDS)
    assert not (query.GROUP_BY_FIELDS & cp.PRIVATE_FIELDS)


# --- fuzzy value correction -----------------------------------------------------------------

_VOCAB = {"neighborhood": ("Great Kills", "Annadale", "Eltingville"),
          "property_type": ("sf_detached", "condo")}


def test_case_insensitive_exact_match_is_corrected():
    safe = cp.sanitize_spec(_one("neighborhood", "=", "great kills"), vocab=_VOCAB)
    assert safe.filters == [{"field": "neighborhood", "op": "=", "value": "Great Kills"}]
    assert safe.corrections == (("neighborhood", "great kills", "Great Kills"),)
    assert any(d.startswith("fuzzy_corrected") for d in safe.dropped)


def test_a_one_typo_value_is_corrected_at_the_cutoff():
    safe = cp.sanitize_spec(_one("neighborhood", "=", "Anadale"), vocab=_VOCAB)
    assert safe.filters[0]["value"] == "Annadale"
    assert safe.corrections == (("neighborhood", "Anadale", "Annadale"),)


def test_below_cutoff_values_pass_through_untouched():
    safe = cp.sanitize_spec(_one("neighborhood", "=", "Brooklyn Heights"), vocab=_VOCAB)
    assert safe.filters[0]["value"] == "Brooklyn Heights"
    assert safe.corrections == ()


def test_fuzzy_never_touches_contains_or_comparison_ops():
    safe = cp.sanitize_spec(_one("neighborhood", "contains", "anadale"), vocab=_VOCAB)
    assert safe.filters[0]["value"] == "anadale"
    assert safe.corrections == ()


def test_fuzzy_corrects_inside_or_groups():
    safe = cp.sanitize_spec(_group("neighborhood", "=", ["great kills", "Anadale"]),
                            vocab=_VOCAB)
    assert safe.filters[0]["values"] == ["Great Kills", "Annadale"]
    assert len(safe.corrections) == 2


def test_private_fields_are_never_fuzzed_because_the_field_check_runs_first():
    hostile_vocab = {"my_notes": ("divorce sale",)}
    safe = cp.sanitize_spec(_one("my_notes", "=", "divorce sale"), vocab=hostile_vocab)
    assert safe.filters == []
    assert safe.corrections == ()
    assert any(d.startswith("field_not_available") for d in safe.dropped)


def test_no_vocab_means_byte_identical_pre_fuzzy_behavior():
    with_none = cp.sanitize_spec(_one("neighborhood", "=", "great kills"))
    assert with_none.filters[0]["value"] == "great kills"
    assert with_none.corrections == ()
    assert not any(d.startswith("fuzzy_corrected") for d in with_none.dropped)


# --- the point of all of it --------------------------------------------------------------------

def test_a_sanitized_spec_always_executes():
    """The contract: nothing sanitize_spec returns can make query.build raise."""
    store = Store(":memory:")
    for i in range(3):
        store.upsert({"street_address": f"{i} Test Street", "zip": "10308",
                      "list_price": 700000 + i, "status": "active"}, "fixture")

    hostile = [
        _spec(limit=-1), _spec(limit="abc"), _one("list_price", "<", None),
        _one("my_notes", "contains", "x"), _one("list_price", "<", "cheap"),
        _spec(sort="'; DROP TABLE listings; --"), _one("zip", "OR 1=1", "1"),
        None, "junk", {"filters": [{"field": None, "op": None, "value": None}]},
        _group("neighborhood", "=", "not-a-list"),
        _group("neighborhood", "=", [["nested"], {"a": 1}, None]),
        _group("zip", "=", ["10308'; DROP TABLE listings; --"]),
        _spec(aggregate="'; DROP TABLE listings; --", aggregate_field="list_price"),
        _spec(aggregate="count", group_by="'; DROP TABLE listings; --"),
    ]
    for spec in hostile:
        safe = cp.sanitize_spec(spec)
        rows = query.run(store, filters=safe.filters, sort=safe.sort,
                         order=safe.order, limit=safe.limit)
        assert len(rows) <= cp.MAX_LIMIT
    assert store.count() == 3, "table damaged"
    store.close()


def test_sanitizing_bounds_the_result_set():
    store = Store(":memory:")
    for i in range(30):
        store.upsert({"street_address": f"{i} Test Street", "zip": "10308",
                      "list_price": 700000 + i, "status": "active"}, "fixture")
    safe = cp.sanitize_spec(_spec(limit=-1))
    assert len(query.run(store, filters=safe.filters, sort=safe.sort,
                         order=safe.order, limit=safe.limit)) == 1
    store.close()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — model specs are clamped, private columns unreachable")
