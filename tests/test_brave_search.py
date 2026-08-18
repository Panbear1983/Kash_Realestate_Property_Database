#!/usr/bin/env python3
"""Offline contract tests for the opt-in Brave Search adapter.

Every HTTP interaction is an injected fake transport. These tests must never contact Brave.
"""
import json
import os
import pwd
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import brave_search, chat, reasoning_contract as rc, web_research as wr  # noqa: E402


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def read(self):
        return self.payload

    def close(self):
        pass


class FakeTransport:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return FakeResponse(json.dumps(payload).encode("utf-8"))


def _payload(prefix, count=3):
    return {"web": {"results": [
        {"title": f"{prefix} title {i}", "url": f"https://{prefix}.example/{i}",
         "description": f"{prefix} snippet {i}"}
        for i in range(1, count + 1)
    ]}}


def _route(queries):
    return {"route": "web_research", "reply": "", "filters": [], "sort": "rank",
            "order": "asc", "limit": 20, "web_queries": queries}


class FakeModel:
    chosen = "fake"

    def __init__(self):
        self.calls = []

    def available(self):
        return True, ""

    def query_spec(self, prompt, schema):
        self.calls.append((prompt, schema))
        if schema is rc.ROUTE_SCHEMA:
            return _route(["current first query", "current second query"])
        return {"reply": "Only the supplied sources support this answer."}


def test_factory_without_key_is_unavailable_and_never_calls_transport():
    transport = FakeTransport([])
    provider = brave_search.provider_from_env({}, transport=transport)
    ok, reason = provider.available()
    assert ok is False and "configured" in reason
    assert isinstance(provider, wr.UnavailableWebProvider)
    assert transport.calls == []


def test_default_secret_path_ignores_a_launchd_style_home_override():
    old_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = "/private/tmp/not-peters-home"
        expected = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config" / "kash" / "brave.env"
        assert brave_search._default_secret_file() == expected
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home


def test_factory_accepts_injected_keychain_reader_without_persisting_a_key():
    transport = FakeTransport([_payload("keychain", 1)])
    provider = brave_search.provider_from_env(
        {}, transport=transport, keychain_reader=lambda: "test-key",
    )
    assert provider.available() == (True, "")
    assert len(provider.search("current rates", limit=1)) == 1
    assert len(transport.calls) == 1


def test_owner_only_local_secret_file_is_accepted_and_permissive_file_is_rejected():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "brave.env"
        path.write_text("BRAVE_SEARCH_API_KEY=test-key\n", encoding="utf-8")
        os.chmod(path, 0o600)
        assert brave_search._key_from_local_secret_file(path) == "test-key"
        os.chmod(path, 0o644)
        assert brave_search._key_from_local_secret_file(path) == ""


def test_factory_accepts_injected_local_secret_reader_without_persisting_a_key():
    transport = FakeTransport([_payload("local", 1)])
    provider = brave_search.provider_from_env(
        {}, transport=transport, secret_file_reader=lambda: "test-key",
    )
    assert provider.available() == (True, "")
    assert len(provider.search("current rates", limit=1)) == 1
    assert len(transport.calls) == 1


def test_brave_request_uses_fixed_endpoint_subscription_header_and_timeout():
    transport = FakeTransport([_payload("rates", 1)])
    provider = brave_search.provider_from_env(
        {"BRAVE_SEARCH_API_KEY": "test-key"}, transport=transport,
    )
    results = provider.search("rates & lenders", limit=99)
    assert len(results) == 1
    assert len(transport.calls) == 1
    request, timeout = transport.calls[0]
    assert request.full_url == (
        "https://api.search.brave.com/res/v1/web/search?"
        "count=3&q=rates+%26+lenders"
    )
    assert request.get_method() == "GET"
    assert request.get_header("X-subscription-token") == "test-key"
    assert request.get_header("Accept") == "application/json"
    assert 0 < timeout <= brave_search.REQUEST_TIMEOUT_SECONDS


def test_brave_default_transport_passes_timeout_as_a_keyword():
    calls = []
    original = brave_search.urlopen
    try:
        def fake_urlopen(request, *, timeout):
            calls.append((request, timeout))
            return FakeResponse(json.dumps(_payload("default", 1)).encode("utf-8"))

        brave_search.urlopen = fake_urlopen
        provider = brave_search.provider_from_env({"BRAVE_SEARCH_API_KEY": "test-key"})
        assert len(provider.search("current rates", limit=1)) == 1
    finally:
        brave_search.urlopen = original
    assert len(calls) == 1
    assert 0 < calls[0][1] <= brave_search.REQUEST_TIMEOUT_SECONDS


def test_brave_valid_response_normalizes_to_web_results():
    transport = FakeTransport([_payload("mortgage", 2)])
    provider = brave_search.provider_from_env(
        {"BRAVE_SEARCH_API_KEY": "test-key"}, transport=transport,
    )
    results = provider.search("current mortgage rates", limit=2)
    assert [(r.title, r.url, r.snippet) for r in results] == [
        ("mortgage title 1", "https://mortgage.example/1", "mortgage snippet 1"),
        ("mortgage title 2", "https://mortgage.example/2", "mortgage snippet 2"),
    ]
    for result in results:
        assert isinstance(result, wr.WebResult)
        assert datetime.fromisoformat(result.fetched_at).tzinfo == timezone.utc


def test_brave_malformed_and_http_error_responses_fail_closed():
    malformed = FakeTransport([{"web": {"results": "not-a-list"}}])
    provider = brave_search.provider_from_env(
        {"BRAVE_SEARCH_API_KEY": "test-key"}, transport=malformed,
    )
    assert provider.search("current rates", limit=3) == []

    error = FakeTransport([HTTPError(brave_search.BRAVE_SEARCH_ENDPOINT, 503, "down", {}, None)])
    provider = brave_search.provider_from_env(
        {"BRAVE_SEARCH_API_KEY": "test-key"}, transport=error,
    )
    assert provider.search("current rates", limit=3) == []
    assert len(error.calls) == 1


def test_chat_uses_no_key_factory_safely_without_any_network_call():
    old = os.environ.pop("BRAVE_SEARCH_API_KEY", None)
    original = brave_search.urlopen
    original_file_reader = brave_search._key_from_local_secret_file
    original_keychain_reader = brave_search._key_from_keychain
    calls = []
    try:
        brave_search.urlopen = lambda *_args, **_kwargs: calls.append(True)
        brave_search._key_from_local_secret_file = lambda: ""
        brave_search._key_from_keychain = lambda: ""
        reply = chat._web_reply(
            "What are the latest mortgage rates?",
            rc.parse(_route(["latest mortgage rates"])),
            backend=FakeModel(), provider=None,
        )
    finally:
        brave_search.urlopen = original
        brave_search._key_from_local_secret_file = original_file_reader
        brave_search._key_from_keychain = original_keychain_reader
        if old is not None:
            os.environ["BRAVE_SEARCH_API_KEY"] = old
    assert reply.kind == "web_unavailable"
    assert calls == []


def test_brave_results_integrate_with_bounded_citations():
    transport = FakeTransport([_payload("first"), _payload("second")])
    provider = brave_search.provider_from_env(
        {"BRAVE_SEARCH_API_KEY": "test-key"}, transport=transport,
    )
    model = FakeModel()
    reply = chat._web_reply(
        "Compare current mortgage rates and lenders",
        rc.parse(_route(["current first query", "current second query"])),
        backend=model, provider=provider,
    )
    assert reply.kind == "web_research"
    assert len(transport.calls) == wr.MAX_QUERIES
    assert reply.text.count("https://") == wr.MAX_TOTAL_RESULTS
    assert "[5] second title 2" in reply.text
    assert "[6]" not in reply.text
    assert len(model.calls) == 1


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed — Brave Search is opt-in, bounded, and offline-tested")
