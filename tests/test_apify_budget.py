#!/usr/bin/env python3
"""Apify budget line: parse the limits endpoint, degrade silently, alert near the cap.

The free tier is a HARD $5/month — past it, actor runs fail. The budget line exists so
drift is visible before that; these tests pin that observability can never itself take a
run down, and that the health alert fires exactly once per level. No network.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import apify_budget, health  # noqa: E402

PAYLOAD = {"data": {"current": {"monthlyUsageUsd": 3.546132},
                    "limits": {"maxMonthlyUsageUsd": 5}}}


class R:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def get_returning(payload):
    return lambda url, params=None, timeout=None: R(payload)


def test_the_limits_payload_parses():
    b = apify_budget.month_to_date(token="t", request_get=get_returning(PAYLOAD))
    assert b == {"used": 3.546132, "cap": 5.0, "pct": 3.546132 / 5.0}
    assert apify_budget.format_line(b) == "apify: $3.55 of $5.00 used this cycle (71%)"


def test_an_unwrapped_payload_also_parses():
    b = apify_budget.month_to_date(token="t", request_get=get_returning(PAYLOAD["data"]))
    assert b and b["cap"] == 5.0


def test_every_failure_mode_degrades_to_none_silently():
    cases = [
        get_returning({}),                                        # schema change
        get_returning({"data": {"current": {}, "limits": {}}}),   # missing keys
        get_returning({"data": {"current": {"monthlyUsageUsd": 1},
                                "limits": {"maxMonthlyUsageUsd": 0}}}),  # zero cap
        lambda *a, **k: (_ for _ in ()).throw(OSError("network down")),
    ]
    for request_get in cases:
        assert apify_budget.month_to_date(token="t", request_get=request_get) is None


def test_no_token_means_no_call_and_none():
    old = os.environ.pop("APIFY_TOKEN", None)
    try:
        boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called"))
        assert apify_budget.month_to_date(request_get=boom) is None
    finally:
        if old is not None:
            os.environ["APIFY_TOKEN"] = old


# --- the health tripwire --------------------------------------------------------------------

def _result(pct):
    return {"summaries": [{"source": "zillow", "fetched": 10, "inserted": 1}],
            "enrich": {"processed": 1, "errors": 0}, "detail": {}, "describe": {}, "rank": {},
            "backup": "/tmp/x.db", "completeness": {"alert_blocked": 0},
            "apify_budget": {"used": pct * 5, "cap": 5.0, "pct": pct}}


def test_ninety_percent_usage_degrades_the_run():
    h = health.assess(_result(0.92))
    assert h["verdict"] == health.DEGRADED
    assert any("Apify credit 92%" in p for p in h["problems"])


def test_below_the_threshold_is_quiet():
    assert health.assess(_result(0.71))["verdict"] == health.OK


def test_rising_percentages_stay_one_alert_not_a_nightly_series():
    """signature() folds the changing numbers, so 92% tonight and 97% tomorrow do not
    re-alert; the weekly still-broken reminder covers the ongoing state."""
    sigs = {health.signature(health.assess(_result(p))["problems"][0])
            for p in (0.90, 0.92, 0.97, 0.99)}
    assert len(sigs) == 1


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
