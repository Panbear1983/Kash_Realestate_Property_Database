#!/usr/bin/env python3
"""Second-round fetch — go back for the fields the first pass didn't get.

    python run_backfill.py --report            # audit only, no calls, no writes
    python run_backfill.py                     # everything due, per the ledger
    python run_backfill.py --field flood_zone  # one field
    python run_backfill.py --force             # ignore backoff (after fixing a source)
    python run_backfill.py --limit 20          # cap work this run

Distinct from the nightly cycle on purpose: when a source that was down comes back, you can
sweep the backlog immediately instead of waiting for 07:00 and 15 rows a night.

Backed by kash/ledger.py, so a source that is still failing is retried on a widening schedule
rather than on every row of every run, and by kash/completeness.py, which owns the definition
of which fields are machine-owned and which of them block Telegram alerts.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from kash import completeness as comp          # noqa: E402
from kash.enrich import enrich, enrich_details, extract_descriptions  # noqa: E402
from kash.ledger import Ledger                  # noqa: E402
from kash.store import Store                    # noqa: E402

DEFAULT_DB = os.path.join(HERE, "pool.db")
DEFAULT_PREFS = os.path.join(HERE, "preferences.yaml")

# Which filler to run for a requested field. Several fields share one filler, so a request for
# any of them runs it once.
FILLERS = {
    "enrich.geocode": "enrich",
    "enrich.flood": "enrich",
    "geo_static": "enrich",          # neighbourhoods are filled at the end of enrich()
    "enrich.detail": "detail",
    "enrich.describe": "describe",
    "rank": "rank",
}


def load_prefs(path):
    if not os.path.exists(path):
        return {}
    from kash import preferences
    return preferences.load(path)


def load_dotenv(path):
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def main():
    ap = argparse.ArgumentParser(description="Kash second-round field backfill")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--prefs", default=DEFAULT_PREFS)
    ap.add_argument("--field", help="backfill one field (e.g. flood_zone)")
    ap.add_argument("--limit", type=int, default=None, help="cap rows per filler this run")
    ap.add_argument("--force", action="store_true", help="ignore ledger backoff")
    ap.add_argument("--report", action="store_true", help="audit only; make no calls")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("No pool.db yet — run:  python run_fetch.py --source mock")
        return

    load_dotenv(os.path.join(HERE, ".env"))
    prefs = load_prefs(args.prefs)
    store = Store(args.db, finance_cfg=prefs.get("finance"))
    ledger = Ledger(store)

    if args.field and args.field not in comp.FIELD_SPEC:
        ap.error(f"unknown field '{args.field}'. Known: {', '.join(sorted(comp.MACHINE_FIELDS))}")

    before = comp.audit(store, ledger)
    print(comp.format_report(before, verbose=True))

    if args.report:
        store.close()
        return

    if args.force:
        # A source that was down and is now fixed shouldn't wait out its own backoff.
        store.conn.execute("UPDATE enrichment_ledger SET attempts=0, last_attempt=NULL "
                           "WHERE resolved_at IS NULL")
        store.conn.commit()
        print("\n[--force] cleared backoff on all unresolved entries")

    # Decide which fillers to run: the one owning --field, or every filler with an open gap.
    if args.field:
        wanted = {FILLERS.get(comp.FIELD_SPEC[args.field]["filler"])}
    else:
        wanted = {FILLERS.get(f) for f, n in before["by_filler"].items() if n}
    wanted.discard(None)

    print(f"\nrunning: {', '.join(sorted(wanted)) or '(nothing outstanding)'}")
    results = {}

    if "enrich" in wanted:
        results["enrich"] = enrich(store, limit=args.limit, ledger=ledger)
    if "detail" in wanted:
        results["detail"] = enrich_details(
            store, limit=args.limit or (prefs.get("detail") or {}).get("max_per_run", 15),
            prefs=prefs)
    if "describe" in wanted:
        results["describe"] = extract_descriptions(
            store, prefs,
            limit=args.limit or (prefs.get("describe") or {}).get("max_per_run", 15))
    if "rank" in wanted:
        from kash import rank
        results["rank"] = rank.rank_new(
            store, prefs, limit=args.limit or (prefs.get("rank") or {}).get("max_per_run", 15))

    for name, stats in results.items():
        print(f"  {name}: {stats}")

    after = comp.audit(store, ledger)
    print()
    print(comp.format_report(after, verbose=True))

    gained = {f: before["fields"][f]["scraped_missing"] - after["fields"][f]["scraped_missing"]
              for f in after["fields"]}
    filled = {f: n for f, n in gained.items() if n > 0}
    print()
    print(f"filled this run: {filled or 'nothing'}")
    print(f"alert-blocked:   {before['alert_blocked']} -> {after['alert_blocked']}")
    store.close()


if __name__ == "__main__":
    main()
