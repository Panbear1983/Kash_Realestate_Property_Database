#!/usr/bin/env python3
"""Offline regression tests for bounded local-market versus external-market comparisons."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import chat, reasoning_contract as rc, web_research as wr  # noqa: E402
from kash.access import Access                                       # noqa: E402
from kash.store import Store                                         # noqa: E402

PREFS = {"chat": {"enabled": True, "rate_limit_per_minute": 20}}
USER = 8001
PRIVATE_NOTE = "PRIVATE_LOCAL_SENTINEL"


class FakeModel:
    name = "fake"
    chosen = "fake"
    last_usage = None

    def __init__(self, response):
        self.response = response
        self.calls = []

    def available(self):
        return True, ""

    def query_spec(self, prompt, schema):
        self.calls.append((prompt, schema))
        assert schema is rc.WEB_ANSWER_SCHEMA
        return self.response


class FakeWeb:
    def __init__(self):
        self.calls = []

    def available(self):
        return True, ""

    def search(self, query, *, limit):
        self.calls.append((query, limit))
        return [wr.WebResult(
            title="NY State listing report",
            url="https://source.example/ny-listings",
            fetched_at="2026-08-03T00:00:00Z",
            snippet="The reported New York State excluding New York City average active-listing benchmark is $650,000 for August 2026.",
        )]


def _store():
    store = Store(":memory:")
    for address, price in (("11 Local Lane", 600000), ("12 Local Lane", 800000)):
        store.upsert({
            "street_address": address,
            "zip": "10301",
            "list_price": price,
            "status": "active",
            "my_notes": PRIVATE_NOTE,
        }, "fixture")
    Access(store).add(USER, "Comparison Tester")
    return store


def test_clear_mixed_comparison_returns_local_aggregate_and_cited_benchmark_not_rows():
    """Regression for the dashboard symptom: aggregate/comparison, never a listing dump."""
    store = _store()
    model = FakeModel({"reply": "The source reports a $650,000 statewide benchmark."})
    web = FakeWeb()
    chat.reset_throttle()
    reply = chat.handle(
        USER,
        "T",
        ("Compare the average active list price of Staten Island properties in the database "
         "with the current average active list price in New York State excluding New York City."),
        store=store,
        prefs=PREFS,
        backend=model,
        web_provider=web,
    )

    assert reply.kind == "market_comparison"
    assert reply.rows == (), "a comparison must not carry raw listings into the dashboard"
    assert "Local database" in reply.text
    assert "$700,000" in reply.text
    assert "2 active listings" in reply.text
    assert "$650,000" in reply.text
    assert "Sources:" in reply.text and "https://source.example/ny-listings" in reply.text
    assert "11 Local Lane" not in reply.text and "12 Local Lane" not in reply.text
    assert PRIVATE_NOTE not in reply.text
    assert len(web.calls) == 1 and web.calls[0][1] == wr.MAX_RESULTS_PER_QUERY
    assert len(model.calls) == 1
    synthesis_prompt = model.calls[0][0]
    for forbidden in ("11 Local Lane", "12 Local Lane", "600000", "800000", PRIVATE_NOTE):
        assert forbidden not in synthesis_prompt


def test_ambiguous_nyc_state_and_price_metric_asks_before_database_or_web_access():
    """Do not guess whether the user means NYC or NY State, or list versus sale price."""
    class Bomb:
        def __getattribute__(self, name):
            raise AssertionError(f"ambiguous comparison accessed dependency: {name}")

    chat.reset_throttle()
    reply = chat.handle(
        USER,
        "T",
        "What is the average of SI property in the database with the average cost in the rest of the NYC state?",
        store=Bomb(),
        prefs=PREFS,
        backend=Bomb(),
        web_provider=Bomb(),
    )
    assert reply.kind == "clarify"
    assert "new york state" in reply.text.lower()
    assert "new york city" in reply.text.lower()


def test_natural_si_versus_new_york_state_defaults_to_active_list_prices():
    store = _store()
    model = FakeModel({"reply": "The source reports a $650,000 statewide benchmark."})
    web = FakeWeb()
    chat.reset_throttle()
    reply = chat.handle(
        USER,
        "T",
        "What is the average cost of Staten Island properties versus New York State?",
        store=store,
        prefs=PREFS,
        backend=model,
        web_provider=web,
    )
    assert reply.kind == "market_comparison"
    assert "$700,000" in reply.text
    assert "active listing prices" in reply.text
    assert "New York State" in reply.text
    assert web.calls == [("current average active listing price New York State", wr.MAX_RESULTS_PER_QUERY)]


def test_mismatched_sale_price_benchmark_is_rejected_before_model_synthesis():
    class SalePriceWeb(FakeWeb):
        def search(self, query, *, limit):
            self.calls.append((query, limit))
            return [wr.WebResult(
                title="New York State median home sales price",
                url="https://source.example/ny-sales",
                fetched_at="2026-08-03T00:00:00Z",
                snippet="June median closed sale price was $475,000.",
            )]

    chat.reset_throttle()
    model = FakeModel({"reply": "must not be called"})
    reply = chat.handle(USER, "T", "What is the average cost of Staten Island properties versus New York State?",
                        store=_store(), prefs=PREFS, backend=model, web_provider=SalePriceWeb())
    assert reply.kind == "benchmark_unavailable"
    assert "won't mix" in reply.text.lower()
    assert model.calls == []


def test_sale_price_comparison_clarifies_without_accessing_any_dependency():
    class Bomb:
        def __getattribute__(self, name):
            raise AssertionError(f"sale-price comparison accessed dependency: {name}")

    chat.reset_throttle()
    reply = chat.handle(
        USER,
        "T",
        "Compare the average sale price of Staten Island with New York State.",
        store=Bomb(),
        prefs=PREFS,
        backend=Bomb(),
        web_provider=Bomb(),
    )
    assert reply.kind == "clarify"
    assert "closed-sale" in reply.text


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — market comparisons are aggregate-only, cited, and offline")
