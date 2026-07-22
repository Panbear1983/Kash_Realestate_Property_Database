#!/usr/bin/env python3
"""Kash terminal UI — talk to the property pool.

    python shell.py

Commands:
  filter <expr>   e.g.  filter tier=A neighborhood~Kills list_price<=750000 sort:list_price limit:10
  sort/top helpers via filter's  sort:FIELD order:desc limit:N  tokens
  show <address|property_id>      full record
  stats                            pool summary
  changes                          recent price/status changes
  ask <question>                   natural-language mode (Claude; needs anthropic + key)
  count | help | quit
"""
import os
import statistics
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from kash import nl, query           # noqa: E402
from kash.store import Store          # noqa: E402

DB = os.path.join(HERE, "pool.db")
ENV = os.path.join(HERE, ".env")

DISPLAY = [
    ("rank", "#", 4), ("tier", "T", 3), ("view_priority", "PRIO", 5),
    ("neighborhood", "NEIGHBORHOOD", 14), ("street_address", "ADDRESS", 22),
    ("list_price", "PRICE", 8), ("beds", "BD", 3), ("baths", "BA", 3),
    ("sqft", "SQFT", 5), ("price_per_sqft", "$/SF", 5),
    ("status", "STATUS", 8), ("source", "SRC", 6),
]


def load_dotenv(path):
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def fmt(v, w):
    s = "" if v is None else str(v)
    return s[:w].ljust(w)


def print_rows(rows):
    if not rows:
        print("  (no matches)")
        return
    header = " ".join(h.ljust(w) for _, h, w in DISPLAY)
    print(header)
    print("-" * len(header))
    for r in rows:
        print(" ".join(fmt(r.get(k), w) for k, _, w in DISPLAY))
    print(f"  {len(rows)} row(s)")


def show_one(store, term):
    term = term.strip()
    if not term:
        print("usage: show <address or property_id>")
        return
    field = "property_id" if term.upper().startswith("SI-") else "street_address"
    op = "=" if field == "property_id" else "contains"
    rows = query.run(store, filters=[{"field": field, "op": op, "value": term}], limit=1)
    if not rows:
        print("  (not found)")
        return
    r = rows[0]
    for k, v in r.items():
        if v not in (None, ""):
            print(f"  {k:20} {v}")


def show_stats(store):
    rows = store.all()
    print(f"  listings:   {len(rows)}")
    prices = [r["list_price"] for r in rows if r.get("list_price")]
    if prices:
        print(f"  price:      min {min(prices):,}  median {int(statistics.median(prices)):,}  max {max(prices):,}")
    for label, field in (("status", "status"), ("tier", "tier"), ("source", "source")):
        c = Counter(r.get(field) or "-" for r in rows)
        print(f"  by {label}:  " + "  ".join(f"{k}:{n}" for k, n in sorted(c.items())))


def show_changes(store):
    for e in store.recent_changes(20):
        print(f"  [{e['ts']}] {e['event']}: {e['detail']}  ({e['match_key']})")


HELP = __doc__


def main():
    load_dotenv(ENV)
    if not os.path.exists(DB):
        print("No pool.db yet — run:  python run_fetch.py --source mock")
        return
    store = Store(DB)
    ok, why = nl.available()
    nl_note = "codex/ChatGPT" if ok else f"off ({why})"
    print(f"Kash shell — {store.count()} listings.  NL mode: {nl_note}.  Type 'help'.")
    while True:
        try:
            line = input("kash> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        cmd, _, rest = line.partition(" ")
        cmd = cmd.lower()
        if cmd in ("quit", "exit", "q"):
            break
        elif cmd == "help":
            print(HELP)
        elif cmd == "count":
            print(store.count())
        elif cmd == "stats":
            show_stats(store)
        elif cmd == "changes":
            show_changes(store)
        elif cmd == "show":
            show_one(store, rest)
        elif cmd in ("filter", "find", "ls"):
            try:
                filters, sort, order, limit = query.parse_command(rest)
                print_rows(query.run(store, filters=filters, sort=sort, order=order, limit=limit))
            except ValueError as e:
                print("  error:", e)
        elif cmd == "ask":
            try:
                msg, rows = nl.answer(rest, store)
                print("  " + msg)
                print_rows(rows)
            except RuntimeError as e:
                print(f"  {e}\n  -> enable Codex/ChatGPT: run 'codex login' (uses your subscription)")
            except Exception as e:  # noqa: BLE001
                print("  NL error:", e)
        else:
            print("  unknown command; type 'help'")
    store.close()


if __name__ == "__main__":
    main()
