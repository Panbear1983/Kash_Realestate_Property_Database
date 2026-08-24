#!/usr/bin/env python3
"""Stateful clarify (slot-filling): the router asks ONE question, holds the partial search,
and the user's next bare answer completes it — deterministically when possible, via the
router with replayed context otherwise. All offline; the merge path is proven zero-model
with a Bomb() backend."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile

from kash import chat, chat_vocabulary, reasoning_contract as rc  # noqa: E402
from kash.access import Access                                    # noqa: E402
from kash.chat_sessions import SessionStore                       # noqa: E402
from kash.readonly import ReadOnlyStore                           # noqa: E402
from kash.store import Store                                      # noqa: E402

PREFS = {"chat": {"enabled": True, "rate_limit_per_minute": 100}}
USER = 8101
OTHER = 8102
QUESTION = "Which neighborhood and what budget?"


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
        return self.responses.pop(0)


class Bomb:
    def __getattribute__(self, name):
        raise AssertionError(f"isolated dependency accessed: {name}")


def _store():
    store = Store(":memory:")
    for hood, beds, price in (("Great Kills", "3", 650000), ("Great Kills", "4", 780000),
                              ("Annadale", "3", 700000), ("Eltingville", "2", 550000)):
        store.upsert({"street_address": f"{beds} {hood} St {price}", "zip": "10308",
                      "neighborhood": hood, "beds": beds, "list_price": price,
                      "status": "active"}, "fixture")
    Access(store).add(USER, "Pending Tester")
    return store


def _sessions():
    directory = tempfile.TemporaryDirectory()
    return directory, SessionStore(os.path.join(directory.name, "sessions.sqlite"))


def _ask_clarify(store, sessions, message="find me a 3-bed"):
    """Drive one model-routed clarify that holds beds=3 and asks QUESTION."""
    model = FakeModel(_route("clarify", reply=QUESTION,
                             filters=[{"field": "beds", "op": "=", "value": "3"}]))
    chat.reset_throttle()
    reply = chat.handle(USER, "T", message, store=store, prefs=PREFS, backend=model,
                        session_store=sessions)
    assert reply.kind == "clarify"
    assert QUESTION in reply.text
    return model


def test_a_model_clarify_with_partial_filters_saves_a_pending():
    store, (directory, sessions) = _store(), _sessions()
    with directory:
        _ask_clarify(store, sessions)
        pending = sessions.consume_pending(USER)
        assert pending is not None
        assert pending["filters"] == [{"field": "beds", "op": "=", "value": "3"}]
        assert pending["question"] == QUESTION


def test_a_bare_money_answer_completes_the_search_with_zero_model_calls():
    store, (directory, sessions) = _store(), _sessions()
    with directory:
        _ask_clarify(store, sessions)
        reply = chat.handle(USER, "T", "under 700k", store=store, prefs=PREFS,
                            backend=Bomb(), session_store=sessions)
        assert reply.kind == "query", reply.text
        assert len(reply.rows) == 2                       # both 3-beds at <= 700000
        assert {r["neighborhood"] for r in reply.rows} == {"Great Kills", "Annadale"}


def test_a_bare_vocabulary_answer_completes_the_search_with_zero_model_calls():
    store, (directory, sessions) = _store(), _sessions()
    with directory:
        chat_vocabulary.reset_cache()
        chat_vocabulary.maybe_refresh(ReadOnlyStore(store), {})
        _ask_clarify(store, sessions)
        reply = chat.handle(USER, "T", "Great Kills", store=store, prefs=PREFS,
                            backend=Bomb(), session_store=sessions)
        chat_vocabulary.reset_cache()
        assert reply.kind == "query", reply.text
        assert len(reply.rows) == 1
        assert reply.rows[0]["beds"] == "3"
        assert reply.rows[0]["neighborhood"] == "Great Kills"


def test_an_answer_on_the_same_field_replaces_the_partial_not_ands_it():
    store, (directory, sessions) = _store(), _sessions()
    with directory:
        model = FakeModel(_route("clarify", reply=QUESTION,
                                 filters=[{"field": "list_price", "op": "<=",
                                           "value": "600000"}]))
        chat.reset_throttle()
        chat.handle(USER, "T", "something cheap", store=store, prefs=PREFS, backend=model,
                    session_store=sessions)
        reply = chat.handle(USER, "T", "under 800k", store=store, prefs=PREFS,
                            backend=Bomb(), session_store=sessions)
        assert reply.kind == "query"
        assert len(reply.rows) == 4                      # 800k governs; ANDing the stale
        #                                                  600k partial would leave only 1


def test_an_unrelated_message_routes_normally_with_the_question_as_context_once():
    store, (directory, sessions) = _store(), _sessions()
    with directory:
        _ask_clarify(store, sessions)
        model = FakeModel(_route("general_reasoning", reply="A cap rate is NOI / price."))
        reply = chat.handle(USER, "T", "what is a cap rate", store=store, prefs=PREFS,
                            backend=model, session_store=sessions)
        assert reply.kind == "general_reasoning"
        prompt = model.calls[0][0]
        assert QUESTION in prompt                         # the router sees what it asked
        assert "beds = 3" in prompt
        # The pending was consumed: the NEXT message carries no pending context.
        model2 = FakeModel(_route("general_reasoning", reply="Sure."))
        chat.handle(USER, "T", "thanks, explain closing costs", store=store, prefs=PREFS,
                    backend=model2, session_store=sessions)
        assert QUESTION not in model2.calls[0][0]


def test_a_dead_pending_does_not_resurrect_for_a_later_bare_answer():
    store, (directory, sessions) = _store(), _sessions()
    with directory:
        _ask_clarify(store, sessions)
        model = FakeModel(_route("general_reasoning", reply="A cap rate is NOI / price."))
        chat.handle(USER, "T", "what is a cap rate", store=store, prefs=PREFS,
                    backend=model, session_store=sessions)
        # "under 700k" now must NOT complete the long-gone search deterministically.
        model2 = FakeModel(_route("clarify", reply="Under 700k for what kind of search?"))
        reply = chat.handle(USER, "T", "under 700k", store=store, prefs=PREFS,
                            backend=model2, session_store=sessions)
        assert reply.kind == "clarify"
        assert len(model2.calls) == 1


def test_pendings_do_not_cross_users():
    store, (directory, sessions) = _store(), _sessions()
    with directory:
        Access(store).add(OTHER, "Someone Else")
        _ask_clarify(store, sessions)
        assert sessions.consume_pending(OTHER) is None
        assert sessions.consume_pending(USER) is not None


def test_pre_route_and_fail_closed_clarifies_never_create_pendings():
    store, (directory, sessions) = _store(), _sessions()
    with directory:
        chat.reset_throttle()
        # A blocklisted request clarifies deterministically — no model, no pending.
        reply = chat.handle(USER, "T", "delete the database listing", store=store,
                            prefs=PREFS, backend=Bomb(), session_store=sessions)
        assert reply.kind == "clarify"
        assert sessions.consume_pending(USER) is None
        # A clarify with NO understood filters holds nothing either.
        model = FakeModel(_route("clarify", reply="Could you say more?"))
        chat.handle(USER, "T", "hmm", store=store, prefs=PREFS, backend=model,
                    session_store=sessions)
        assert sessions.consume_pending(USER) is None


def test_the_session_file_never_stores_the_users_message_text():
    store, (directory, sessions) = _store(), _sessions()
    with directory:
        marker = "EXTREMELY DISTINCT USER PHRASE 9932"
        _ask_clarify(store, sessions, message=f"find me a 3-bed {marker}")
        blob = open(sessions.path, "rb").read().decode("utf-8", "ignore")
        assert marker not in blob
        assert QUESTION in blob                # the MODEL-authored question is what's held


def test_bare_answer_shapes_map_to_the_right_filters():
    pending = {"filters": [{"field": "beds", "op": "=", "value": "3"}],
               "sort": "rank", "order": "asc", "limit": 20, "question": "?"}
    assert chat._pending_answer("under 700k", pending) == {
        "field": "list_price", "op": "<=", "value": "700000"}
    assert chat._pending_answer("$750,000", pending) == {
        "field": "list_price", "op": "<=", "value": "750000"}
    assert chat._pending_answer("at least 500k", pending) == {
        "field": "list_price", "op": ">=", "value": "500000"}
    assert chat._pending_answer("2 baths", pending) == {
        "field": "baths", "op": "=", "value": "2"}
    assert chat._pending_answer("4 bedrooms", pending) == {
        "field": "beds", "op": "=", "value": "4"}
    assert chat._pending_answer("3", pending) is None          # ambiguous bare number
    assert chat._pending_answer("what about schools?", pending) is None
    assert chat._pending_answer("x" * 61, pending) is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — pending clarifications merge once, safely, or die")
