#!/usr/bin/env python3
"""Detail-scrape batch construction: only URLs the actor accepts, with ledger backoff.

The Apify detail actor rejects `/homes/<address>/` URLs ("Invalid URL … Unknown URL format",
from its own run log) — and because nothing got filled, the identical address-row batch was
retried every night while zpid rows queued behind it. The actor is faked; no network.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.enrich import detail                 # noqa: E402
from kash.dedup import match_key                # noqa: E402
from kash.ledger import Ledger                  # noqa: E402
from kash.store import Store                    # noqa: E402

ZPID_URL = "https://www.zillow.com/homedetails/{slug}/{z}_zpid/"
ADDR_URL = "https://www.zillow.com/homes/{slug}/"


def row(i, zpid=None):
    slug = f"{i}-Test-Ave-Staten-Island-NY-10308"
    url = ZPID_URL.format(slug=slug, z=zpid) if zpid else ADDR_URL.format(slug=slug)
    return {"street_address": f"{i} Test Ave", "zip": "10308", "list_price": 700000,
            "beds": "3", "baths": "2", "property_type": "sf_detached", "listing_url": url}


def store_with(rows):
    s = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    for r in rows:
        s.upsert(r, source="test")
    return s


class FakeActor:
    """Replaces requests.post; records what URLs were sent and answers for given zpids."""

    def __init__(self, answers=None, fail=None):
        self.sent, self._answers, self._fail = [], answers or {}, fail

    def __call__(self, url, params=None, json=None, timeout=None):
        self.sent = [u["url"] for u in json["startUrls"]]
        if self._fail:
            raise self._fail
        items = []
        for u in self.sent:
            z = detail._zpid(u)
            if z and z in self._answers:
                items.append({"zpid": z, "url": u, **self._answers[z]})

        class R:
            def raise_for_status(self):
                pass

            def json(self, _items=items):
                return _items
        return R()


def run(store, fake, limit=15):
    old = detail.requests.post
    detail.requests.post = fake
    try:
        os.environ.setdefault("APIFY_TOKEN", "test-token")
        return detail.enrich_details(store, limit=limit)
    finally:
        detail.requests.post = old


# --- batch construction ---------------------------------------------------------------------

def test_address_urls_are_never_sent_to_the_actor():
    s = store_with([row(1), row(2, zpid="111"), row(3)])
    fake = FakeActor(answers={"111": {"yearBuilt": 1970}})
    out = run(s, fake)
    assert fake.sent == [ZPID_URL.format(slug="2-Test-Ave-Staten-Island-NY-10308", z="111")]
    assert out["awaiting_zpid_url"] == 2


def test_an_all_address_pool_sends_nothing_and_spends_nothing():
    s = store_with([row(i) for i in range(5)])
    fake = FakeActor()
    out = run(s, fake)
    assert out == {"detailed": 0, "candidates": 0, "awaiting_zpid_url": 5}
    assert fake.sent == []


def test_a_successful_row_is_filled_and_resolved_in_the_ledger():
    s = store_with([row(1, zpid="111")])
    out = run(s, FakeActor(answers={"111": {"yearBuilt": 1970}}))
    assert out["detailed"] == 1
    assert s.all()[0]["year_built"] == 1970
    entry = Ledger(s).entry(match_key(row(1, zpid="111")), detail.LEDGER_FIELD)
    assert entry["resolved_at"], "a filled row must close its ledger entry"


def test_a_failed_run_ledgers_every_batched_row():
    s = store_with([row(1, zpid="111"), row(2, zpid="222")])
    out = run(s, FakeActor(fail=RuntimeError("400 Bad Request")))
    assert out["detailed"] == 0 and "error" in out
    lg = Ledger(s)
    for i, z in ((1, "111"), (2, "222")):
        entry = lg.entry(match_key(row(i, zpid=z)), detail.LEDGER_FIELD)
        assert entry and entry["attempts"] == 1 and not entry["resolved_at"]


def test_repeatedly_failing_rows_sink_behind_fresh_ones():
    """The poison-pill fix: attempts order the queue, so limit slots go to fresh rows."""
    s = store_with([row(1, zpid="111"), row(2, zpid="222")])
    lg = Ledger(s)
    lg.record_failure(match_key(row(1, zpid="111")), detail.LEDGER_FIELD, "boom")
    lg.record_failure(match_key(row(1, zpid="111")), detail.LEDGER_FIELD, "boom")
    fake = FakeActor(answers={"222": {"yearBuilt": 1980}})
    run(s, fake, limit=1)
    assert len(fake.sent) == 1 and "222_zpid" in fake.sent[0], \
        "the never-tried row must take the single batch slot"


def test_an_unanswered_row_is_recorded_as_a_failure():
    s = store_with([row(1, zpid="111"), row(2, zpid="222")])
    run(s, FakeActor(answers={"111": {"yearBuilt": 1970}}))   # 222 returns nothing
    entry = Ledger(s).entry(match_key(row(2, zpid="222")), detail.LEDGER_FIELD)
    assert entry and entry["attempts"] == 1 and not entry["resolved_at"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} passed")
