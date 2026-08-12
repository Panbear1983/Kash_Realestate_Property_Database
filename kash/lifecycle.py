"""Listings that quietly went away — ageing a row to off_market on real evidence only.

The naive version of this feature ("not seen in N days -> off_market") would be wrong here,
and wrong in the expensive direction: it would bury still-active houses the buyer wants to
see. Neither provider returns a complete picture of the market:

* Zillow (Apify) returns `results_limit` results per search, sorted newest-first. A run that
  comes back with exactly 40 of a possible 40 has told us nothing about listing #41.
* RentCast returns up to 100 per ZIP across ALL prices and types, so our price/beds slice is
  a small part of a truncated window — it never proves absence and does not report coverage.

So absence is only evidence when a query came back UNDER its cap: the provider then handed us
everything matching that query, and a pooled row inside that same slice which did not appear
is genuinely no longer listed there. That is the only condition under which this module marks
anything, and it is why `coverage()` reports `truncated`.

Even then it waits for `misses_before_off_market` (default 2) independent covering runs, and
it never touches:

* rows sourced from anywhere but the covering provider (curated/imported rows have no live
  sighting to miss),
* rows already pending / sold / off_market (a pending listing legitimately leaves search),
* rows the buyer is working — favorited, viewing booked, offer in flight, agent contacted.

Nothing here is one-way: if the listing turns up again, `Store.upsert` flips it back to active
and increments `times_relisted`, and the next sighting clears the miss counter.
"""
from __future__ import annotations

from datetime import date

DEFAULT_MISSES = 2

# Most rows this may age in one run. The pool holds 55 zillow rows in the top price band
# alone, so a single systematically-bad scrape (the actor subdivides the map into quadrants
# and could under-return one) would otherwise bury dozens of live houses in one night. The
# backlog drains over consecutive runs instead, staying visible in the digest as it goes.
DEFAULT_MAX_AGED_PER_RUN = 10

# Workflow state that means "the buyer is engaged with this house" — never age these out
# from under them, even if the provider stops returning the listing.
_ENGAGED_VIEWING = {"scheduled", "seen"}
_ENGAGED_OFFER = {"considering", "offered", "accepted"}


def _first_number(value):
    """Beds/baths arrive as text, including ranges like '3-4' (kept on their low end)."""
    import re
    if value is None:
        return None
    m = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(m.group()) if m else None


def provider_of(row: dict) -> str:
    """The provider that supplied a row: 'zillow' from 'zillow', 'seed' from 'seed:docx'."""
    return str(row.get("source") or "").split(":")[0]


def is_engaged(row: dict) -> bool:
    return bool(
        row.get("favorite")
        or row.get("contacted_agent")
        or str(row.get("viewing_status") or "") in _ENGAGED_VIEWING
        or str(row.get("offer_status") or "") in _ENGAGED_OFFER
    )


def in_coverage(row: dict, cov: dict) -> bool:
    """Was this pooled row inside the slice the query actually asked for?

    Anything the query filtered on that the row cannot answer (no price, no beds) counts as
    NOT covered: we can only claim a row is missing from a result set it would have been in.
    """
    if provider_of(row) != cov.get("source"):
        return False

    price = row.get("list_price")
    if cov.get("price_min") is not None or cov.get("price_max") is not None:
        if price is None:
            return False
        if cov.get("price_min") is not None and price < cov["price_min"]:
            return False
        if cov.get("price_max") is not None and price > cov["price_max"]:
            return False

    for field, key in (("beds", "beds_min"), ("baths", "baths_min")):
        floor = cov.get(key)
        if floor:
            val = _first_number(row.get(field))
            if val is None or val < float(floor):
                return False

    excluded = {str(t).lower() for t in (cov.get("excluded_types") or ())}
    if excluded:
        ptype = str(row.get("property_type") or "").lower()
        if not ptype or ptype in excluded:
            return False

    zips = cov.get("zips")
    if zips:
        z = str(row.get("zip") or "")
        if not z or z not in {str(v) for v in zips}:
            return False
    return True


def is_ageable(row: dict, cov: dict) -> bool:
    """Could this row legitimately be aged out if the covering run did not return it?"""
    if str(row.get("status") or "active") != "active":
        return False        # pending/sold/off_market rows leave search results honestly
    if is_engaged(row):
        return False
    return in_coverage(row, cov)


class Lifecycle:
    """Miss counters, kept beside the pool in its own table (like the enrichment ledger)."""

    def __init__(self, store):
        self.store = store
        self.conn = store.conn
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS listing_sightings (
                match_key TEXT PRIMARY KEY,
                misses INTEGER DEFAULT 0,
                last_miss TEXT,
                last_covered TEXT
            )"""
        )
        self.conn.commit()

    def misses(self, key: str) -> int:
        row = self.conn.execute(
            "SELECT misses FROM listing_sightings WHERE match_key=?", (key,)).fetchone()
        return int(row[0]) if row else 0

    def entry(self, key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT match_key, misses, last_miss, last_covered FROM listing_sightings "
            "WHERE match_key=?", (key,)).fetchone()
        cols = ["match_key", "misses", "last_miss", "last_covered"]
        return dict(zip(cols, row)) if row else None

    def record_miss(self, key: str, today: str) -> int:
        n = self.misses(key) + 1
        self.conn.execute(
            """INSERT INTO listing_sightings (match_key, misses, last_miss, last_covered)
               VALUES (?,?,?,?)
               ON CONFLICT(match_key) DO UPDATE SET
                   misses=excluded.misses, last_miss=excluded.last_miss,
                   last_covered=excluded.last_covered""",
            (key, n, today, today))
        return n

    def record_sighting(self, key: str, today: str) -> None:
        """Seen again — the counter resets, so misses must be consecutive to add up."""
        self.conn.execute(
            """INSERT INTO listing_sightings (match_key, misses, last_miss, last_covered)
               VALUES (?,0,NULL,?)
               ON CONFLICT(match_key) DO UPDATE SET
                   misses=0, last_miss=NULL, last_covered=excluded.last_covered""",
            (key, today))


def age_listings(store, prefs: dict, coverages: list[dict], seen_keys, today=None) -> dict:
    """Apply one run's coverage to the pool. Returns stats for the run report.

    `coverages` holds one entry per source query this run; truncated ones are ignored (they
    prove nothing). `seen_keys` is every match_key merged this run, from any source.
    """
    today = today or date.today().isoformat()
    cfg = prefs.get("lifecycle") or {}
    if cfg.get("enabled") is False:
        return {"skipped": "disabled"}
    threshold = int(cfg.get("misses_before_off_market", DEFAULT_MISSES))
    reported = [c for c in coverages if c]
    conclusive = [c for c in reported if not c.get("truncated")]
    stats = {"covered": 0, "missed": 0, "aged": 0,
             "truncated_runs": len(reported) - len(conclusive)}
    if not conclusive:
        return stats

    seen = set(seen_keys or ())
    lc = Lifecycle(store)
    aged_rows = []
    # store.all() projects FIELD_ORDER only, which does not include the key, so walk the keys
    # and read each row through the public getter.
    keys = [r[0] for r in store.conn.execute("SELECT match_key FROM listings").fetchall()]
    for key in keys:
        row = store.get(key)
        if row is None:
            continue
        if not any(is_ageable(row, cov) for cov in conclusive):
            continue
        stats["covered"] += 1
        if key in seen or row.get("fetched_at") == today:
            lc.record_sighting(key, today)
            continue
        n = lc.record_miss(key, today)
        stats["missed"] += 1
        if n >= threshold:
            aged_rows.append((key, row, n))

    # Longest-unseen first, so the clearest cases go before the borderline ones.
    aged_rows.sort(key=lambda t: (-t[2], t[1].get("fetched_at") or ""))
    cap = int(cfg.get("max_aged_per_run", DEFAULT_MAX_AGED_PER_RUN))
    stats["deferred"] = max(0, len(aged_rows) - cap)
    for key, row, n in aged_rows[:cap]:
        store.update_fields(key, {"status": "off_market"})
        # Same event the merge path writes, so this reaches the digest's "gone" group and the
        # changelog reads the same whether a provider said so or absence did.
        store._log(key, "status_change",
                   f"{row.get('street_address')}: active -> off_market "
                   f"(absent from {n} complete searches)", "lifecycle")
        stats["aged"] += 1
    store.conn.commit()
    return stats


def format_stats(stats: dict) -> str:
    if stats.get("skipped"):
        return f"lifecycle: {stats['skipped']}"
    if not stats.get("covered"):
        capped = stats.get("truncated_runs", 0)
        why = (f"{capped} search(es) hit their result cap" if capped
               else "no source reported complete coverage")
        return f"lifecycle: nothing to age — {why}"
    line = (f"lifecycle: {stats['covered']} covered, {stats['missed']} absent, "
            f"{stats['aged']} aged to off_market")
    if stats.get("deferred"):
        line += f" ({stats['deferred']} held back by the per-run cap)"
    return line
