#!/usr/bin/env python3
"""The LLM fallback ladder: rung selection, fall-through, and probe caching.

Fake backends throughout — no network, no codex subprocess, no API key needed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import llm  # noqa: E402

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


class FakeBackend:
    """Stands in for a real rung. Counts probes so we can assert the TTL cache works."""

    def __init__(self, name, ok=True, why="", raises=None, spec=None):
        self.name = name
        self._ok, self._why, self._raises = ok, why, raises
        self._spec = spec if spec is not None else {"from": name}
        self.probes = 0
        self.calls = 0

    def available(self):
        self.probes += 1
        return self._ok, self._why

    def query_spec(self, prompt, schema):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._spec


def _ladder(*fakes):
    """Build a LadderBackend whose rungs are the given fakes, in order."""
    by_name = {f.name: f for f in fakes}
    rung = llm.LadderBackend({}, [f.name for f in fakes])
    rung._rungs = lambda: ((n, by_name[n]) for n in rung.names)
    return rung, by_name


def setup_function(_):
    llm.reset_cache()


# --- rung selection ------------------------------------------------------------------------

def test_healthy_first_rung_answers_and_second_is_untouched():
    rung, by = _ladder(FakeBackend("anthropic"), FakeBackend("codex"))
    assert rung.query_spec("q", SCHEMA) == {"from": "anthropic"}
    assert rung.chosen == "anthropic"
    assert by["codex"].calls == 0


def test_unavailable_first_rung_falls_through_to_second():
    rung, by = _ladder(
        FakeBackend("anthropic", ok=False, why="ANTHROPIC_API_KEY not set"),
        FakeBackend("codex"),
    )
    assert rung.query_spec("q", SCHEMA) == {"from": "codex"}
    assert rung.chosen == "codex"
    assert by["anthropic"].calls == 0      # never even attempted


def test_raising_first_rung_falls_through_to_second():
    rung, by = _ladder(
        FakeBackend("anthropic", raises=RuntimeError("503 overloaded")),
        FakeBackend("codex"),
    )
    assert rung.query_spec("q", SCHEMA) == {"from": "codex"}
    assert rung.chosen == "codex"
    assert by["anthropic"].calls == 1      # it was tried, then abandoned


def test_all_rungs_down_raises_and_names_every_reason():
    rung, _ = _ladder(
        FakeBackend("anthropic", ok=False, why="no key"),
        FakeBackend("codex", raises=RuntimeError("not logged in")),
    )
    try:
        rung.query_spec("q", SCHEMA)
    except RuntimeError as e:
        assert "no key" in str(e) and "not logged in" in str(e)
    else:
        raise AssertionError("expected RuntimeError when every rung is down")


def test_available_is_true_when_any_rung_is_up():
    rung, _ = _ladder(FakeBackend("anthropic", ok=False, why="no key"), FakeBackend("codex"))
    assert rung.available() == (True, "")


# --- ladder resolution ---------------------------------------------------------------------

def test_auto_walks_the_configured_ladder():
    assert llm.resolve_ladder({"backend": "auto", "ladder": ["anthropic", "codex"]}) == [
        "anthropic", "codex"]


def test_explicit_backend_pins_one_rung_and_beats_the_ladder():
    cfg = {"backend": "codex", "ladder": ["anthropic", "codex"]}
    assert llm.resolve_ladder(cfg) == ["codex"]


def test_per_user_override_beats_the_configured_backend():
    cfg = {"backend": "auto", "ladder": ["anthropic", "codex"]}
    assert llm.resolve_ladder(cfg, "codex") == ["codex"]


def test_unknown_backend_name_is_rejected():
    try:
        llm.resolve_ladder({"backend": "gpt9"})
    except ValueError as e:
        assert "gpt9" in str(e)
    else:
        raise AssertionError("expected ValueError for an unknown backend name")


def test_unknown_names_in_ladder_are_skipped_not_fatal():
    assert llm.resolve_ladder({"backend": "auto", "ladder": ["mystery", "codex"]}) == ["codex"]


# --- per-job ladders -----------------------------------------------------------------------

JOB_CFG = {
    "backend": "auto",
    "ladders": {"chat": ["codex", "claude_cli"],
                "rank": ["claude_cli", "agy_cli"]},
    "ladder": ["codex"],
}


def test_each_job_gets_its_own_ladder():
    assert llm.resolve_ladder(JOB_CFG, job="chat") == ["codex", "claude_cli"]
    assert llm.resolve_ladder(JOB_CFG, job="rank") == ["claude_cli", "agy_cli"]


def test_unnamed_job_falls_back_to_the_shared_ladder():
    assert llm.resolve_ladder(JOB_CFG, job="extract") == ["codex"]
    assert llm.resolve_ladder(JOB_CFG) == ["codex"]


def test_override_still_beats_the_job_ladder():
    assert llm.resolve_ladder(JOB_CFG, "agy_cli", job="chat") == ["agy_cli"]


def test_pinned_backend_beats_the_job_ladder():
    cfg = {**JOB_CFG, "backend": "codex"}
    assert llm.resolve_ladder(cfg, job="rank") == ["codex"]


def test_route_threads_job_through():
    assert llm.route(JOB_CFG, job="rank").names == ["claude_cli", "agy_cli"]
    assert llm.route(JOB_CFG, job="chat").names == ["codex", "claude_cli"]


# --- lenient JSON parsing (agy has no schema flag) -------------------------------------------

def test_loads_loose_handles_bare_json():
    assert llm._loads_loose('{"a": 1}') == {"a": 1}


def test_loads_loose_strips_markdown_fence():
    assert llm._loads_loose('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm._loads_loose('```\n{"a": 1}\n```') == {"a": 1}


def test_loads_loose_digs_json_out_of_prose():
    assert llm._loads_loose('Sure! Here you go:\n{"a": 1}\nHope that helps.') == {"a": 1}


def test_loads_loose_returns_none_when_unparseable():
    assert llm._loads_loose("no json here") is None
    assert llm._loads_loose("") is None
    assert llm._loads_loose(None) is None


def test_empty_config_defaults_to_codex_alone():
    assert llm.resolve_ladder({}) == ["codex"]
    assert llm.resolve_ladder(None) == ["codex"]


# --- per-backend config --------------------------------------------------------------------

def test_backend_config_inherits_shared_timeout_and_ttl():
    cfg = {"timeout": 90, "available_ttl": 30, "backends": {"codex": {"bin": "codex"}}}
    assert llm.backend_config(cfg, "codex") == {
        "timeout": 90, "available_ttl": 30, "bin": "codex"}


def test_backend_config_lets_a_backend_override_the_shared_timeout():
    cfg = {"timeout": 120, "backends": {"anthropic": {"timeout": 60}}}
    assert llm.backend_config(cfg, "anthropic")["timeout"] == 60


def test_legacy_flat_config_still_supplies_the_model():
    """Pre-ladder shape: llm: {backend: codex, model: o3, timeout: 120}."""
    cfg = {"backend": "codex", "model": "o3", "timeout": 120}
    assert llm.backend_config(cfg, "codex")["model"] == "o3"


# --- probe caching -------------------------------------------------------------------------

def test_probe_is_cached_within_the_ttl_window():
    calls = []

    def probe():
        calls.append(1)
        return True, ""

    for _ in range(5):
        assert llm._cached("codex", 60, probe) == (True, "")
    assert len(calls) == 1


def test_zero_ttl_disables_caching():
    calls = []

    def probe():
        calls.append(1)
        return True, ""

    llm._cached("codex", 0, probe)
    llm._cached("codex", 0, probe)
    assert len(calls) == 2


def test_reset_cache_forces_a_fresh_probe():
    calls = []

    def probe():
        calls.append(1)
        return True, ""

    llm._cached("codex", 60, probe)
    llm.reset_cache()
    llm._cached("codex", 60, probe)
    assert len(calls) == 2


def test_invalidate_marks_a_rung_unavailable_for_the_window():
    llm._cached("anthropic", 60, lambda: (True, ""))
    llm._invalidate("anthropic", "AuthenticationError: bad key")
    ok, why = llm._cached("anthropic", 60, lambda: (True, ""))
    assert ok is False and "AuthenticationError" in why


# --- real backends, no network -------------------------------------------------------------

def test_anthropic_rung_reports_unavailable_without_a_key():
    saved_key = os.environ.pop("ANTHROPIC_API_KEY", None)
    saved_env = llm._ensure_env
    llm._ensure_env = lambda: None          # don't let .env files put a key back
    try:
        ok, why = llm.AnthropicBackend({"available_ttl": 0}).available()
        assert ok is False
        assert "ANTHROPIC_API_KEY" in why or "anthropic SDK" in why
    finally:
        llm._ensure_env = saved_env
        if saved_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved_key


def test_get_backend_still_works_for_rank_with_auto_config():
    """kash.rank calls get_backend(prefs['llm']); `auto` must not read as an unknown name."""
    backend = llm.get_backend({"backend": "auto", "ladder": ["codex"]})
    assert backend.names == ["codex"]


if __name__ == "__main__":
    # Mirrors the other test modules: runnable without pytest installed.
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        setup_function(None)
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
