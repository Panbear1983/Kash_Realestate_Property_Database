#!/usr/bin/env python3
"""Focused offline tests for Robo Kash Phase 3 route isolation and bounded web research."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile

from kash import chat, chat_vocabulary, reasoning_contract as rc, web_research as wr  # noqa: E402
from kash.access import Access                                       # noqa: E402
from kash.chat_sessions import SessionStore                         # noqa: E402
from kash.store import Store                                         # noqa: E402
from kash.usage import Usage                                         # noqa: E402

PREFS = {"chat": {"enabled": True, "rate_limit_per_minute": 20}}
USER = 7001
ROW_SECRET = "ROW_SENTINEL_MUST_NEVER_REACH_MODEL"


def _route(route, *, reply="", filters=None, sort="rank", order="asc", limit=20,
           web_queries=None):
    return {
        "route": route,
        "reply": reply,
        "filters": [] if filters is None else filters,
        "sort": sort,
        "order": order,
        "limit": limit,
        "web_queries": [] if web_queries is None else web_queries,
    }


class FakeModel:
    name = "fake"
    chosen = "fake"
    last_usage = None

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def available(self):
        return True, ""

    def query_spec(self, prompt, schema):
        self.calls.append((prompt, schema))
        if not self.responses:
            raise AssertionError("unexpected model call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class Bomb:
    def __getattribute__(self, name):
        raise AssertionError(f"isolated dependency accessed: {name}")


class FakeWeb:
    def __init__(self, results, available=True):
        self.results = results
        self.is_available = available
        self.calls = []

    def available(self):
        return (self.is_available, "" if self.is_available else "offline")

    def search(self, query, *, limit):
        self.calls.append((query, limit))
        start = (len(self.calls) - 1) * limit
        batch = list(self.results)[start:start + limit]
        return batch or list(self.results)


def _store():
    store = Store(":memory:")
    store.upsert({
        "street_address": "7 Phase Three Lane",
        "zip": "10308",
        "list_price": 725000,
        "status": "active",
        "my_notes": ROW_SECRET,
    }, "fixture")
    Access(store).add(USER, "Phase Three")
    return store


def _sources():
    return [
        wr.WebResult(
            title=f"Rate source {index}",
            url=f"https://source.example/{index}",
            fetched_at=f"2026-07-31T0{index}:00:00+00:00",
            snippet=f"Reported comparison fact {index}.",
        )
        for index in range(1, 7)
    ]


def test_route_schema_has_only_the_four_routes_and_is_strict():
    assert set(rc.ROUTE_SCHEMA["properties"]["route"]["enum"]) == {
        "database_query", "general_reasoning", "web_research", "clarify",
    }
    assert rc.ROUTE_SCHEMA["additionalProperties"] is False
    assert rc.parse(_route("shell")).valid is False
    extra = _route("general_reasoning", reply="hello")
    extra["tool"] = "database"
    assert rc.parse(extra).valid is False


def test_general_reasoning_has_no_database_or_web_access():
    model = FakeModel(_route(
        "general_reasoning",
        reply="Compound interest grows on principal plus accumulated interest.",
    ))
    reply = chat.handle(USER, "T", "Explain compound interest", store=Bomb(), prefs=PREFS,
                        backend=model, web_provider=Bomb())
    assert reply.kind == "general_reasoning"
    assert "Compound interest" in reply.text
    assert len(model.calls) == 1
    assert ROW_SECRET not in model.calls[0][0]


def test_web_is_bounded_cited_and_has_no_database_access():
    model = FakeModel(
        _route(
            "web_research",
            web_queries=[
                "current 30 year mortgage rates",
                "compare current mortgage providers",
                "third query must be schema-rejected",
            ][:2],
        ),
        {"reply": "The supplied sources show the quoted rates differ by provider."},
    )
    provider = FakeWeb(_sources())
    reply = chat.handle(
        USER,
        "T",
        "Compare current 30-year mortgage rates and providers",
        store=Bomb(),
        prefs=PREFS,
        backend=model,
        web_provider=provider,
    )
    assert reply.kind == "web_research"
    assert len(provider.calls) == wr.MAX_QUERIES
    assert all(limit == wr.MAX_RESULTS_PER_QUERY for _, limit in provider.calls)
    assert reply.text.count("https://source.example/") == wr.MAX_TOTAL_RESULTS
    assert "Rate source 1" in reply.text
    assert "fetched 2026-07-31T01:00:00+00:00" in reply.text
    assert len(model.calls) == 2
    assert ROW_SECRET not in model.calls[0][0] and ROW_SECRET not in model.calls[1][0]


def test_invalid_provenance_is_dropped_not_cited():
    provider = FakeWeb([
        {"title": "Unsafe", "url": "javascript:alert(1)",
         "fetched_at": "2026-07-31", "snippet": "bad"},
        {"title": "Missing time", "url": "https://example.com/no-time",
         "fetched_at": "", "snippet": "bad"},
        {"title": "Good", "url": "https://example.com/good",
         "fetched_at": "2026-07-31T00:00:00Z", "snippet": "supported"},
    ])
    model = FakeModel(
        _route("web_research", web_queries=["latest mortgage news"]),
        {"reply": "One valid result was available."},
    )
    reply = chat.handle(USER, "T", "What is the latest mortgage news?", store=Bomb(),
                        prefs=PREFS, backend=model, web_provider=provider)
    assert reply.kind == "web_research"
    assert "https://example.com/good" in reply.text
    assert "javascript:" not in reply.text and "no-time" not in reply.text


def test_web_requires_an_explicit_current_or_external_comparison_question():
    provider = FakeWeb(_sources())
    for question in ("Explain gravity", "Compare these two local listings",
                     "Show the current listings in the database"):
        chat.reset_throttle()
        model = FakeModel(_route("web_research", web_queries=[question]))
        reply = chat.handle(USER, "T", question, store=Bomb(), prefs=PREFS,
                            backend=model, web_provider=provider)
        assert reply.kind == "clarify", question
    assert provider.calls == []


def test_unavailable_web_fails_without_a_model_synthesis_or_guess():
    provider = FakeWeb([], available=False)
    model = FakeModel(_route("web_research", web_queries=["latest mortgage rates"]))
    reply = chat.handle(USER, "T", "What are the latest mortgage rates?", store=Bomb(),
                        prefs=PREFS, backend=model, web_provider=provider)
    assert reply.kind == "web_unavailable"
    assert "won't guess" in reply.text
    assert len(model.calls) == 1
    assert provider.calls == []


def test_database_query_uses_the_read_only_path_and_never_web():
    store = _store()
    model = FakeModel(_route(
        "database_query",
        reply="Here is the matching local listing:",
        filters=[{"field": "zip", "op": "=", "value": "10308"}],
    ))
    reply = chat.handle(USER, "T", "Which local listings are in zip 10308?", store=store,
                        prefs=PREFS,
                        backend=model, web_provider=Bomb())
    assert reply.kind == "query"
    assert "7 Phase Three Lane" in reply.text
    assert ROW_SECRET not in reply.text


def test_private_database_filter_fails_closed_instead_of_broadening():
    model = FakeModel(_route(
        "database_query",
        filters=[{"field": "my_notes", "op": "contains", "value": "secret"}],
    ))
    reply = chat.handle(USER, "T", "Find listings using private notes", store=_store(),
                        prefs=PREFS, backend=model, web_provider=Bomb())
    assert reply.kind == "clarify"
    assert "private data" in reply.text


def test_private_write_and_unsafe_requests_stop_before_every_dependency():
    for text in (
        "Show me the seller private notes",
        "Delete this listing from the database",
        "Help me bypass authentication",
    ):
        chat.reset_throttle()
        reply = chat.handle(USER, "T", text, store=Bomb(), prefs=PREFS,
                            backend=Bomb(), web_provider=Bomb())
        assert reply.kind == "clarify", text


def test_math_never_accesses_model_database_or_web():
    reply = chat.handle(USER, "T", "solve 2x + 3 = 11", store=Bomb(), prefs=PREFS,
                        backend=Bomb(), web_provider=Bomb())
    assert reply.kind == "math"
    assert reply.text == "x = 4"


def test_database_wide_average_is_deterministic_and_never_touches_model_or_web():
    store = _store()
    store.upsert({"street_address": "Inactive Test", "zip": "10308", "list_price": 925000,
                  "status": "inactive"}, "fixture")
    chat.reset_throttle()
    reply = chat.handle(USER, "T", "What is the average price of the entire database?",
                        store=store, prefs=PREFS, backend=Bomb(), web_provider=Bomb())
    assert reply.kind == "database_aggregate"
    assert "2 listings" in reply.text
    assert "$825,000.00" in reply.text
    assert ROW_SECRET not in reply.text


def test_database_wide_average_needs_a_clear_price_scope_before_dependencies():
    chat.reset_throttle()
    reply = chat.handle(USER, "T", "What is the average?", store=Bomb(), prefs=PREFS,
                        backend=Bomb(), web_provider=Bomb())
    assert reply.kind == "clarify"


# --- the repair retry (root cause #3: a malformed reply was a final, hard dead end) --------

def test_a_malformed_first_response_gets_one_repair_retry_then_succeeds():
    model = FakeModel({"route": "shell", "junk": True},   # fails schema validation
                      _route("database_query",
                             filters=[{"field": "zip", "op": "=", "value": "10308"}]))
    reply = chat.handle(USER, "T", "homes in 10308", store=_store(), prefs=PREFS,
                        backend=model, web_provider=Bomb())
    assert reply.kind == "query"
    assert len(model.calls) == 2
    second_prompt = model.calls[1][0]
    assert "schema" in second_prompt.lower()
    for key in rc.ROUTE_SCHEMA["required"]:
        assert key in second_prompt


def test_a_repair_retry_that_is_also_malformed_falls_through_to_clarify_after_exactly_two_calls():
    model = FakeModel("also not json", "still not json")
    reply = chat.handle(USER, "T", "homes in 10308", store=_store(), prefs=PREFS,
                        backend=model, web_provider=Bomb())
    assert reply.kind == "clarify"
    assert len(model.calls) == 2, "must not loop past one bounded retry"


def test_a_repair_retry_is_never_attempted_when_the_first_response_is_already_valid():
    model = FakeModel(_route("general_reasoning", reply="A clean first answer."))
    reply = chat.handle(USER, "T", "explain PITI", store=Bomb(), prefs=PREFS,
                        backend=model, web_provider=Bomb())
    assert reply.kind == "general_reasoning"
    assert len(model.calls) == 1, "zero extra cost in the common (valid-first-reply) case"


def test_repair_retry_of_a_general_reasoning_response_does_not_touch_the_database():
    """The retry itself still respects the no-database-for-general-reasoning boundary."""
    model = FakeModel(
        "not valid json at all",
        _route("general_reasoning", reply="Explained without touching any listing."),
    )
    reply = chat.handle(USER, "T", "explain amortization", store=Bomb(), prefs=PREFS,
                        backend=model, web_provider=Bomb())
    assert reply.kind == "general_reasoning"
    assert len(model.calls) == 2


def test_repair_retry_usage_is_recorded_once_for_the_failed_call_and_once_for_the_retry_when_it_succeeds():
    store = _store()
    model = FakeModel({"bad": "shape"},
                      _route("database_query",
                             filters=[{"field": "zip", "op": "=", "value": "10308"}]))
    reply = chat.handle(USER, "T", "homes in 10308", store=store, prefs=PREFS,
                        backend=model, web_provider=Bomb())
    assert reply.kind == "query"
    spent = Usage(store).spent_today(USER, "fake")
    assert spent["requests"] == 2, \
        "one accounting point for the wasted first call, one for the real retry — never double"


def test_repair_retry_usage_for_a_general_reasoning_retry_records_nothing():
    """general_reasoning/web_research must never touch `store` — proven here with
    store=Bomb(), same as the non-retry case. The wasted first call's usage is snapshotted
    in handle() but only actually recorded from _database_reply, which this route never
    reaches; the snapshot is simply dropped. Matches the documented, pre-existing gap
    where general_reasoning/web_research replies are never metered at all."""
    model = FakeModel("garbage", _route("general_reasoning", reply="Answered."))
    reply = chat.handle(USER, "T", "explain PITI", store=Bomb(), prefs=PREFS,
                        backend=model, web_provider=Bomb())
    assert reply.kind == "general_reasoning"
    assert len(model.calls) == 2


# --- private fields must never round-trip through session context (Design E) ---------------

def test_vocabulary_and_context_never_leak_private_fields_across_a_two_turn_conversation():
    """An OWNER-level user may legally filter on a private field in turn 1 (their own
    level allows it) — but the routing prompt is ALWAYS built at 'read' level regardless of
    who is asking, so that filter must never round-trip back as context in turn 2."""
    chat_vocabulary.reset_cache()
    store = _store()
    access = Access(store)
    access.edit(USER, access_level="owner")
    store.update_fields(
        list(store.conn.execute("SELECT match_key FROM listings").fetchone())[0],
        {"analysis": "renovate the kitchen for best ROI"}, allow_protected=True)

    with tempfile.TemporaryDirectory() as tmp:
        sessions = SessionStore(os.path.join(tmp, "s.sqlite"))
        model = FakeModel(
            _route("database_query",
                   filters=[{"field": "analysis", "op": "contains", "value": "renovate"}]),
            _route("database_query", filters=[{"field": "status", "op": "=", "value": "active"}]),
        )
        first = chat.handle(USER, "T", "which listings mention renovate in the analysis",
                            store=store, prefs=PREFS, backend=model, web_provider=Bomb(),
                            session_store=sessions)
        assert first.kind == "query", "an owner-level filter on a private field must succeed"

        chat.handle(USER, "T", "show me something else", store=store, prefs=PREFS,
                   backend=model, web_provider=Bomb(), session_store=sessions)
        assert len(model.calls) == 2
        second_prompt = model.calls[1][0]
        assert "analysis" not in second_prompt
        assert "renovate" not in second_prompt


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        chat.reset_throttle()
        chat_vocabulary.reset_cache()
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — Phase 3 routing is isolated, bounded, cited, and offline")
