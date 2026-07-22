#!/usr/bin/env python3
"""Phase 4 entry point — enrich the pool with coordinates + FEMA flood zones.

    python run_enrich.py            # enrich all rows that need it
    python run_enrich.py --limit 5  # bound the number of rows (and API calls)

Free, no-key sources (US Census geocoder + FEMA NFHL). Safe to re-run: it skips rows
already filled and never touches your notes, ranking, or source provenance.
"""
import argparse
import os

from kash.enrich import enrich
from kash.store import Store

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "pool.db")


def main():
    ap = argparse.ArgumentParser(description="Kash Phase 4 enrichment")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("No pool.db yet — run:  python run_fetch.py --source mock")
        return
    store = Store(args.db)
    print(f"Enriching {store.count()} listings (limit={args.limit or 'none'})...")
    stats = enrich(store, limit=args.limit)
    print(f"  processed:   {stats['processed']}")
    print(f"  geocoded:    {stats['geocoded']}")
    print(f"  flood-zoned: {stats['flood_zoned']}")
    print(f"  errors:      {stats['errors']}")
    store.close()


if __name__ == "__main__":
    main()
