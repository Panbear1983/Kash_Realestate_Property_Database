#!/usr/bin/env python3
"""Follow-up memory: "those", "the second one", "#3" refer to the last result list.

The safety property matters more than the convenience: a plain new question must NEVER be
silently scoped to old results, sessions must not leak across users, and expiry must fail
open to a normal query. No network; the model is faked; sessions live in a temp file.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import chat, reasoning_contract  # noqa: E402
from kash.access import Access  # noqa: E402
from kash.chat_sessions import SessionStore  # noqa: E402
from kash.dedup import match_key  # noqa: E402
from kash.store import Store  # noqa: E402

USER = 42
PREFS = {"chat": {"enabled": True, "rate_limit_per_minute": 1000},
         "eligibility": {"telegram_min_baths": 2, "telegram_max_price": 800000}}


def _row(i, price):
    return {"street_address": f"{i} Memory Ln", "zip": "10308", "list_price": price,
            "beds": "3", "baths": "2.5", "property_type": "sf_detached",
            "status": "active",     # upsert stores dicts as-is; the pipeline's schema
                                    # default doesn't apply to fixture rows
            "listing_url": f"https://www.zillow.com/homedetails/{i}-Memory-Ln/{i}00_zpid/"}


def _setup():
    store = Store(":memory:")
    for i, price in ((1, 600000), (2, 700000), (3, 750000)):
        r = _row(i, price)
        store.upsert(r, source="zillow")
        store.update_fields(match_key(r), {"rank": i}, allow_protected=True)
    Access(store).ensure_owner([USER])
    sessions = SessionStore(os.path.join(tempfile.mkdtemp(), "s.sqlite"))
    return store, sessions


class FakeBackend:
    """Returns one scripted route spec, shaped exactly as the contract requires. Records
    every prompt it was asked, so a test can inspect what context the router saw."""
    name = "fake"
    chosen = "fake"
    last_usage = None

    def __init__(self, **spec_over):
        base = {k: None for k in reasoning_contract.ROUTE_SCHEMA["required"]}
        base.update({"route": "clarify", "reply": "", "filters": [], "sort": "rank",
                     "order": "asc", "limit": 20, "web_queries": []})
        base.update(spec_over)
        self.spec = base
        self.prompts = []

    def available(self):
        return True, ""

    def query_spec(self, prompt, schema):
        self.prompts.append(prompt)
        return dict(self.spec)


def ask(store, sessions, message, backend=None):
    return chat.handle(USER, "Tester", message, store=store, prefs=PREFS,
                       backend=backend or FakeBackend(), session_store=sessions)


def test_a_successful_query_saves_the_session():
    store, sessions = _setup()
    reply = ask(store, sessions, "show all active listings")
    assert reply.kind == "query"
    sess = sessions.load(USER)
    assert sess and len(sess["listing_keys"]) == 3


def test_the_second_one_picks_from_the_last_list():
    store, sessions = _setup()
    ask(store, sessions, "show all active listings")
    reply = ask(store, sessions, "tell me about the second one")
    assert reply.kind == "query"
    assert "2 Memory Ln" in reply.text and "1 Memory Ln" not in reply.text
    assert "From your last list, #2" in reply.text


def test_numeric_reference_works_too():
    store, sessions = _setup()
    ask(store, sessions, "show all active listings")
    reply = ask(store, sessions, "open #3")
    assert "3 Memory Ln" in reply.text


def test_an_out_of_range_ordinal_clarifies_with_the_list_size():
    store, sessions = _setup()
    ask(store, sessions, "show all active listings")
    reply = ask(store, sessions, "the fifth one")
    assert reply.kind == "clarify" and "3 home(s)" in reply.text


def test_a_refinement_answers_within_the_last_list():
    store, sessions = _setup()
    ask(store, sessions, "show all active listings")
    backend = FakeBackend(route="database_query", reply="Under 720k:",
                          filters=[{"field": "list_price", "op": "<=", "value": 720000}])
    reply = ask(store, sessions, "which of those are under 720k", backend=backend)
    assert "Within your last list" in reply.text
    assert "1 Memory Ln" in reply.text and "2 Memory Ln" in reply.text
    assert "3 Memory Ln" not in reply.text


def test_refinement_chains_the_session_to_the_narrowed_set():
    store, sessions = _setup()
    ask(store, sessions, "show all active listings")
    backend = FakeBackend(route="database_query", reply="Under 720k:",
                          filters=[{"field": "list_price", "op": "<=", "value": 720000}])
    ask(store, sessions, "which of those are under 720k", backend=backend)
    assert len(sessions.load(USER)["listing_keys"]) == 2, \
        "'those' now means the narrowed list"


def test_no_overlap_says_so_instead_of_pretending():
    store, sessions = _setup()
    ask(store, sessions, "show all active listings")
    backend = FakeBackend(route="database_query", reply="Above 900k:",
                          filters=[{"field": "list_price", "op": ">=", "value": 900000}])
    reply = ask(store, sessions, "which of those are above 900k", backend=backend)
    assert reply.kind == "empty_result" or "none of your last list matched" in reply.text


def test_a_plain_question_is_never_scoped_to_old_results():
    """No anaphor -> full-pool answer, even with a live session."""
    store, sessions = _setup()
    ask(store, sessions, "show all active listings")
    backend = FakeBackend(route="database_query", reply="Everything:", filters=[])
    reply = ask(store, sessions, "show homes sorted by price", backend=backend)
    assert "Within your last list" not in reply.text


def test_sessions_do_not_leak_across_users():
    store, sessions = _setup()
    ask(store, sessions, "show all active listings")
    Access(store).ensure_owner([77])
    reply = chat.handle(77, "Other", "the second one", store=store, prefs=PREFS,
                        backend=FakeBackend(), session_store=sessions)
    assert reply.kind == "clarify", "user 77 has no session; must fall through to the model"


def test_an_expired_session_falls_open_to_a_normal_query():
    store, _ = _setup()
    expired = SessionStore(os.path.join(tempfile.mkdtemp(), "s.sqlite"), ttl_seconds=0)
    chat.handle(USER, "Tester", "show all active listings", store=store, prefs=PREFS,
                backend=FakeBackend(), session_store=expired)
    reply = chat.handle(USER, "Tester", "the second one", store=store, prefs=PREFS,
                        backend=FakeBackend(), session_store=expired)
    assert reply.kind == "clarify", "expired memory must not answer"


# --- modification-aware follow-ups: the prior filter list becomes router context -----------
# ("actually make that under 700k" with no anaphor at all — Design E)

def test_saved_session_carries_real_filters_not_just_sort():
    store, sessions = _setup()
    ask(store, sessions, "show all active listings")
    assert sessions.load(USER)["filters"] == [{"field": "status", "op": "=", "value": "active"}]


def test_a_modification_message_gets_prior_filters_as_router_context():
    store, sessions = _setup()
    ask(store, sessions, "show all active listings")
    backend = FakeBackend(route="database_query", reply="Under 700k:",
                          filters=[{"field": "list_price", "op": "<=", "value": 700000}])
    ask(store, sessions, "actually make that under 700k", backend=backend)
    assert len(backend.prompts) == 1
    assert "status = active" in backend.prompts[0]
    assert "OPTIONAL background" in backend.prompts[0]


def test_a_plain_new_question_still_gets_context_the_model_may_ignore():
    """Context is injected regardless of phrasing (no anaphor needed) — safety comes from
    it being explicitly optional in the prompt, not from withholding it."""
    store, sessions = _setup()
    ask(store, sessions, "show all active listings")
    backend = FakeBackend(route="database_query", reply="Cheapest first:",
                          filters=[], sort="list_price")
    ask(store, sessions, "what's the cheapest thing you've got", backend=backend)
    assert "status = active" in backend.prompts[0]
    assert "ignore this context completely" in backend.prompts[0]


def test_stale_session_context_is_not_injected_after_five_minutes():
    """A separate, SHORTER freshness window than the 30-minute session TTL — the ordinal-
    pick feature keeps working for the full 30 minutes; unprompted context stops much
    sooner. Unit-tested directly against _followup_context since chat.handle has no
    injectable clock for session freshness."""
    ttl = 1800
    saved_at = 1_000_000.0
    sess = {"filters": [{"field": "status", "op": "=", "value": "active"}],
            "expires_at": saved_at + ttl, "listing_keys": ("a",)}

    fresh = chat._followup_context(sess, ttl, now=saved_at + 60)
    assert "status = active" in fresh

    boundary = chat._followup_context(sess, ttl, now=saved_at + chat._FOLLOWUP_CONTEXT_TTL_SECONDS)
    assert "status = active" in boundary

    stale = chat._followup_context(sess, ttl, now=saved_at + chat._FOLLOWUP_CONTEXT_TTL_SECONDS + 1)
    assert stale == ""


def test_followup_context_is_empty_with_no_session_or_no_filters():
    assert chat._followup_context(None, 1800) == ""
    assert chat._followup_context({"filters": [], "expires_at": 1000}, 1800, now=500) == ""
    assert chat._followup_context({"filters": None, "expires_at": 1000}, 1800, now=500) == ""


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
