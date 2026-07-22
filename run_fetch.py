#!/usr/bin/env python3
"""Phase 2 entry point — run a source through the fetch pipeline into the pool.

    python run_fetch.py --source mock      # test the whole pipeline, no credentials
    python run_fetch.py --source rentcast  # needs RENTCAST_API_KEY in env/.env
    python run_fetch.py --source zillow     # needs a managed-scraper token (flagged)

First run seeds the pool from the docx CSV, then merges the source's listings in.
"""
import argparse
import os
import sys

from kash.adapters import REGISTRY
from kash.adapters.base import CredentialError
from kash import pipeline, preferences
from kash.store import Store

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CSV = os.path.join(HERE, "archive", "SI_July2026_Active_Properties.csv")
DEFAULT_DB = os.path.join(HERE, "pool.db")
DEFAULT_PREFS = os.path.join(HERE, "preferences.yaml")
DEFAULT_ENV = os.path.join(HERE, ".env")


def load_dotenv(path: str) -> None:
    """Minimal .env loader (no dependency) — sets vars not already in the environment."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    load_dotenv(DEFAULT_ENV)
    ap = argparse.ArgumentParser(description="Kash Phase 2 fetch pipeline")
    ap.add_argument("--source", default="mock", choices=sorted(REGISTRY))
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--prefs", default=DEFAULT_PREFS)
    ap.add_argument("--limit", type=int, default=None, help="override fetch_limit")
    args = ap.parse_args()

    prefs = preferences.ensure(args.prefs, args.csv)
    if args.limit:
        prefs["fetch_limit"] = args.limit

    store = Store(args.db, finance_cfg=prefs.get("finance"))
    if store.count() == 0:
        n = store.seed_from_csv(args.csv)
        print(f"Seeded pool from CSV: {n} rows")

    adapter = REGISTRY[args.source](config=prefs.get("sources", {}).get(args.source, {}))
    try:
        summary = pipeline.run(adapter, store, prefs)
    except CredentialError as e:
        print(f"\n[{args.source}] not configured: {e}\n", file=sys.stderr)
        sys.exit(2)

    print(f"\n=== fetch: {summary['source']} ===")
    for k in ("fetched", "inserted", "updated", "unchanged", "out_of_scope", "rejected"):
        print(f"  {k:12} {summary[k]}")
    print(f"  {'pool_size':12} {summary['pool_size']}")
    if summary["events"]:
        print("  changes this run:")
        for e in summary["events"]:
            print(f"    - {e['event']}: {e['detail']}  ({e['match_key']})")
    store.close()


if __name__ == "__main__":
    main()
