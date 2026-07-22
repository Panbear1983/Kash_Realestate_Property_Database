#!/usr/bin/env python3
"""Export the live pool to CSV on demand.

    python export_csv.py            # -> pool_export.csv (all rows, all fields)

The daily update cycle also writes this automatically. The frozen seed lives in
archive/SI_July2026_Active_Properties.csv and is left untouched.
"""
import os

from kash.export import to_csv
from kash.store import Store

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    db = os.path.join(HERE, "pool.db")
    if not os.path.exists(db):
        print("No pool.db yet — run:  python run_update.py --sources zillow")
        return
    out = os.path.join(HERE, "pool_export.csv")
    store = Store(db)
    n = to_csv(store, out)
    print(f"exported {n} rows -> {out}")
    store.close()


if __name__ == "__main__":
    main()
